import gzip
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pit_crew.lanes.analysis.critical_path import CIProfile, profile
from pit_crew.lanes.analysis.detectors.shard import detect_shard_by_timing
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
    steps = "\n".join(
        f"      - name: {n}\n        run: {commands.get(n, f'echo {i}')}" for i, n in enumerate(step_names)
    )
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
