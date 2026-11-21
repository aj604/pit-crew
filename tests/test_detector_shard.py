import gzip
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pit_crew.lanes.analysis.critical_path import CIProfile, profile
from pit_crew.lanes.analysis.detectors.shard import _is_static_check, detect_shard_by_timing
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
PATH = ".github/workflows/ci.yml"

Raw = dict[str, Any]


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def raw_run(run_id: int, steps: Sequence[tuple[str, float]], job_name: str = "test") -> Raw:
    total = sum(s for _, s in steps) + 10
    return {
        "id": run_id,
        "path": PATH,
        "event": "push",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": at(0),
        "jobs": [
            {
                "id": run_id,
                "name": job_name,
                "run_attempt": 1,
                "conclusion": "success",
                "created_at": at(0),
                "started_at": at(0),
                "completed_at": at(total),
                "labels": [],
                "steps": [
                    {"number": i, "name": n, "conclusion": "success", "started_at": at(0), "completed_at": at(s)}
                    for i, (n, s) in enumerate(steps, 1)
                ],
            }
        ],
    }


def workflow_yaml(step_names: Sequence[str], matrix: str = "", runs: dict[str, str] | None = None) -> str:
    commands = runs or {}
    steps = "\n".join(f"      - name: {n}\n        run: {commands.get(n, n)}" for i, n in enumerate(step_names))
    strategy = f"    strategy:\n      matrix:\n        {matrix}\n" if matrix else ""
    return f"name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n{strategy}    steps:\n{steps}\n"


def build(
    step_seconds: Sequence[tuple[str, float]],
    yaml_steps: Sequence[str] | None = None,
    matrix: str = "",
    runs: dict[str, str] | None = None,
) -> tuple[CIProfile, Workflow]:
    names = yaml_steps if yaml_steps is not None else [n for n, _ in step_seconds]
    wf = parse_workflow(workflow_yaml(names, matrix, runs))
    history = parse_runs([raw_run(i, step_seconds, "test (1)" if matrix else "test") for i in range(1, 4)])
    return profile(wf, history), wf


def test_shard_flags_a_long_test_step() -> None:
    prof, wf = build([("Install", 10.0), ("Run npm test", 240.0)])
    (finding,) = detect_shard_by_timing(prof, wf, PATH)
    assert finding.skill == "shard-by-timing"
    assert (finding.job, finding.step) == ("test", "Run npm test")
    assert finding.evidence["shards"] == 8.0
    assert finding.expected_savings_seconds == 210.0
    assert finding.evidence["step_p50_seconds"] == 240.0
    assert finding.evidence["runs_sampled"] == 3.0
    assert (finding.bucket, finding.risk, finding.confidence) == ("running", "low", 0.6)
    assert "every test file" in finding.description


def test_shard_respects_existing_shard_matrix() -> None:
    prof, wf = build([("Run npm test", 240.0)], matrix="shard: [1, 2, 3]")
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_ignores_short_steps() -> None:
    prof, wf = build([("Run npm test", 59.0)])
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_skips_setup_steps() -> None:
    prof, wf = build([("Install dependencies", 200.0)])
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_ignores_long_non_test_steps() -> None:
    prof, wf = build([("Coverage report", 240.0)], runs={"Coverage report": "uv run coverage html"})
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_flags_a_test_command_under_a_neutral_name() -> None:
    prof, wf = build([("End to end", 240.0)], runs={"End to end": "npx playwright test"})
    (finding,) = detect_shard_by_timing(prof, wf, PATH)
    assert finding.step == "End to end"


def test_shard_skips_unmeasured_steps() -> None:
    prof, wf = build([("Other", 5.0)], yaml_steps=["Run npm test"])
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_empty_profile() -> None:
    wf = parse_workflow(workflow_yaml(["Run npm test"]))
    assert detect_shard_by_timing(profile(wf, []), wf, PATH) == []


def test_shard_fastapi() -> None:
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        raw: list[Raw] = json.load(f)
    wf = parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text())
    prof = profile(wf, parse_runs(raw))
    (finding,) = detect_shard_by_timing(prof, wf, PATH)
    assert (finding.job, finding.step) == ("test", "Test")
    max_saving = next(j.max_saving_seconds for j in prof.jobs if j.job_id == "test")
    assert 0 < finding.expected_savings_seconds <= max_saving
    assert finding.evidence["step_p50_seconds"] == 129.0


def test_shard_matches_the_command_not_the_step_name() -> None:
    prof, wf = build(
        [("Upload test results", 240.0), ("Typecheck", 240.0)],
        runs={"Upload test results": "aws upload results.xml", "Typecheck": "mypy tests/"},
    )
    # neither an upload nor mypy over the tests directory runs the tests
    assert [f.step for f in detect_shard_by_timing(prof, wf, PATH)] == []


def test_shard_ignores_artifact_uploads_named_after_tests() -> None:
    wf = parse_workflow(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: x\n    steps:\n"
        "      - name: Upload test results\n        uses: actions/upload-artifact@v4\n"
    )
    prof = profile(wf, parse_runs([raw_run(i, [("Upload test results", 240.0)]) for i in range(1, 4)]))
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_uses_the_step_samples() -> None:
    prof, wf = build([("Run npm test", 240.0)])
    (finding,) = detect_shard_by_timing(prof, wf, PATH)
    assert finding.evidence["runs_sampled"] == next(s.samples for s in prof.steps if s.name == "Run npm test")


def test_shard_drops_findings_the_job_cap_pushes_under_ten_seconds() -> None:
    wf = parse_workflow(
        "name: CI\non: [push]\njobs:\n"
        "  test:\n    runs-on: x\n    steps:\n      - name: Run tests\n        run: pytest\n"
        "  other:\n    runs-on: x\n    steps:\n      - run: sleep 1\n"
    )
    raw = raw_run(1, [("Run tests", 240.0)])
    slow = {**raw["jobs"][0], "name": "other", "id": 2, "completed_at": at(245), "steps": []}
    raw["jobs"].append(slow)
    prof = profile(wf, parse_runs([{**raw, "id": i} for i in (1, 2, 3)]))
    assert next(j.max_saving_seconds for j in prof.jobs if j.job_id == "test") < 10
    assert detect_shard_by_timing(prof, wf, PATH) == []


@pytest.mark.parametrize("command", ["a=b=c " * 2000 + "pytest", "=" * 60000 + " pytest"])
def test_shard_classifies_adversarial_commands_in_linear_time(command: str) -> None:
    p, wf = build([("Unit", 120.0)], runs={"Unit": f"'{command}'"})
    start = time.perf_counter()
    findings = detect_shard_by_timing(p, wf, PATH)
    assert time.perf_counter() - start < 0.5
    assert [f.job for f in findings] == ["test"]


def test_shard_flags_tox_test_suites() -> None:
    prof, wf = build([("Run tests", 200.0)], runs={"Run tests": "tox -e py312"})
    (finding,) = detect_shard_by_timing(prof, wf, PATH)
    assert finding.step == "Run tests"


def test_shard_name_fallback_flags_an_unknown_test_wrapper() -> None:
    prof, wf = build([("Run tests", 200.0)], runs={"Run tests": "./scripts/ci.sh"})
    assert [f.step for f in detect_shard_by_timing(prof, wf, PATH)] == ["Run tests"]


def test_shard_name_fallback_skips_known_non_test_commands() -> None:
    prof, wf = build(
        [("Upload test results", 240.0), ("Run tests", 240.0)],
        runs={"Upload test results": "aws s3 cp test-results s3://x", "Run tests": "mypy tests/"},
    )
    assert detect_shard_by_timing(prof, wf, PATH) == []


@pytest.mark.parametrize(
    "command",
    [
        'ruff>"$REPORT_DIR/run=$GITHUB_RUN_ID.json" check --output-format json tests/',
        "CI=true ruff check tests/",
        "FOO= BAR=1 ruff check tests/",
    ],
)
def test_shard_treats_lint_commands_as_static_checks(command: str) -> None:
    assert _is_static_check(command)
    prof, wf = build([("Lint tests", 240.0)], runs={"Lint tests": f"'{command}'"})
    assert detect_shard_by_timing(prof, wf, PATH) == []


def test_shard_empty_value_assignments_do_not_hide_the_test_command() -> None:
    assert not _is_static_check("FOO= BAR=1 pytest")
    prof, wf = build([("Unit", 240.0)], runs={"Unit": "FOO= BAR=1 pytest"})
    assert [f.step for f in detect_shard_by_timing(prof, wf, PATH)] == ["Unit"]


@pytest.mark.parametrize(
    ("name", "command"),
    [
        ("Run tests", "sudo aws s3 cp test-results s3://x"),
        ("Run tests", "env AWS_PROFILE=x time nice nohup aws s3 cp test-results s3://x"),
        ("Run tests", "gh run download && ./publish.sh"),
        ("Run tests", "gh release upload v1 dist/*"),
        ("Upload test report", "npx junit-upload report.xml"),
        ("Upload test report", "codecov --file coverage.xml"),
        ("Upload test report", "xargs coveralls"),
        ("Lint tests", '"ruff" check tests/'),
        ("Typecheck tests", "'mypy' tests/"),
    ],
)
def test_shard_ignores_commands_that_only_mention_tests(name: str, command: str) -> None:
    prof, wf = build([(name, 240.0)], runs={name: f"|\n          {command}"})
    assert detect_shard_by_timing(prof, wf, PATH) == []


@pytest.mark.parametrize(
    "command",
    [
        "npx playwright test --shard=1/4",
        "npx playwright test --shard 1/4",
        "npx jest --shard=2/3",
        "pytest --splits 4 --group 1",
        "pytest --splits=4 --group=1",
        "pytest --num-shards 4 --shard-id 0",
        "pytest --num-shards=4",
        "./run-tests --partition 1/3",
        "pytest -n auto --splits 3 --group 2",
        "pytest tests/ --shard-id=2",
        'pytest "--group" 1',
        "pytest -k $CI_NODE_INDEX",
        "pytest --total $CI_NODE_TOTAL",
        'pytest --part "${SHARD_INDEX}"',
        "pytest --part $SPLIT_INDEX",
    ],
)
def test_shard_skips_steps_whose_command_already_shards(command: str) -> None:
    prof, wf = build([("Run tests", 240.0)], runs={"Run tests": f"|\n          {command}"})
    assert detect_shard_by_timing(prof, wf, PATH) == []


@pytest.mark.parametrize("command", ["pytest -n auto", "pytest -n 4 tests/", "pytest --numprocesses=4"])
def test_shard_still_flags_xdist_parallelism_alone(command: str) -> None:
    prof, wf = build([("Run tests", 240.0)], runs={"Run tests": f"|\n          {command}"})
    assert [f.step for f in detect_shard_by_timing(prof, wf, PATH)] == ["Run tests"]


@pytest.mark.parametrize("variable", ["CI_NODE_INDEX", "CI_NODE_TOTAL", "SHARD_INDEX", "SPLIT_INDEX"])
def test_shard_skips_steps_that_take_the_shard_from_env(variable: str) -> None:
    wf = parse_workflow(
        "name: CI\non: [push]\njobs:\n  test:\n    runs-on: x\n    steps:\n"
        f"      - name: Run tests\n        run: pytest\n        env:\n          {variable}: ${{{{ matrix.i }}}}\n"
    )
    prof = profile(wf, parse_runs([raw_run(i, [("Run tests", 240.0)]) for i in range(1, 4)]))
    assert detect_shard_by_timing(prof, wf, PATH) == []


@pytest.mark.parametrize(
    "command",
    [
        "tox -e py312",
        "./scripts/ci.sh",
        "uv run pytest tests/",
        "python -m pytest",
        "cargo test",
        "make test",
        "npx tsc -p tests && npm test",
        "npx playwright test",
        "sudo -E pytest",
        '"pytest" tests/',
    ],
)
def test_shard_keeps_real_test_commands_positive(command: str) -> None:
    prof, wf = build([("Run tests", 240.0)], runs={"Run tests": f"|\n          {command}"})
    assert [f.step for f in detect_shard_by_timing(prof, wf, PATH)] == ["Run tests"]
