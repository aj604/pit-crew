import gzip
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pit_crew.lanes.analysis.critical_path import CIProfile, profile
from pit_crew.lanes.analysis.detectors.dedupe import detect_dedupe_jobs
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import MATRIX_TOO_LARGE, Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"
PATH = ".github/workflows/ci.yml"
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(name: str, seconds: float, index: int = 0) -> dict[str, Any]:
    return {
        "id": index,
        "name": name,
        "run_attempt": 1,
        "conclusion": "success",
        "created_at": at(0),
        "started_at": at(0),
        "completed_at": at(seconds),
        "labels": [],
        "steps": [],
    }


def run(durations: dict[str, float]) -> dict[str, Any]:
    return {
        "id": 1,
        "path": PATH,
        "event": "push",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": at(0),
        "jobs": [job(n, s, i) for i, (n, s) in enumerate(durations.items(), 1)],
    }


def profiled(text: str, durations: dict[str, float]) -> tuple[CIProfile, Workflow]:
    wf = parse_workflow(text)
    return profile(wf, parse_runs([run(durations)])), wf


def test_dedupe_flags_identical_jobs() -> None:
    text = """
jobs:
  test-a:
    runs-on: ubuntu-latest
    steps:
      - name: First name
        run: |
          pytest -q
          coverage report
  test-b:
    runs-on: ubuntu-latest
    steps:
      - name: Other name
        run: |
                pytest -q
                coverage report
"""
    p, wf = profiled(text, {"test-a": 100, "test-b": 200})
    findings = detect_dedupe_jobs(p, wf, PATH)
    assert len(findings) == 1
    f = findings[0]
    assert (f.skill, f.job, f.step, f.bucket, f.risk, f.confidence) == (
        "dedupe-jobs",
        "test-b",
        "",
        "running",
        "medium",
        0.8,
    )
    assert f.evidence["duplicate_of"] == "test-a"
    assert float(f.evidence["max_saving_seconds"]) > 10
    assert f.expected_savings_seconds == f.evidence["max_saving_seconds"]
    assert "keep one" in f.description.lower()
    assert "drop 'test-b' and keep test-a" in f.description
    assert "never remove the check" in f.description.lower()


def test_dedupe_ignores_jobs_that_differ_by_one_command() -> None:
    text = """
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -q
  b:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -x
"""
    p, wf = profiled(text, {"a": 100, "b": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_jobs_without_steps() -> None:
    text = "jobs:\n  a:\n    runs-on: ubuntu-latest\n  b:\n    runs-on: ubuntu-latest\n"
    p, wf = profiled(text, {"a": 100, "b": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_different_runners_and_unmeasured_jobs() -> None:
    text = """
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: make
  b:
    runs-on: macos-latest
    steps:
      - run: make
  c:
    runs-on: ubuntu-latest
    steps:
      - run: make
"""
    wf = parse_workflow(text)
    p = profile(wf, parse_runs([run({"a": 100, "b": 200})]))
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_empty_profile() -> None:
    text = "jobs:\n  a:\n    runs-on: x\n    steps:\n      - run: y\n  b:\n    runs-on: x\n    steps:\n      - run: y\n"
    wf = parse_workflow(text)
    assert detect_dedupe_jobs(profile(wf, []), wf, PATH) == []


def test_dedupe_fastapi_has_no_duplicates() -> None:
    wf = parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text())
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as fh:
        p = profile(wf, parse_runs(json.load(fh)))
    assert detect_dedupe_jobs(p, wf, PATH) == []


def _pair(extra_a: str = "", extra_b: str = "", step_a: str = "- run: make", step_b: str = "- run: make") -> str:
    return f"""
jobs:
  a:
    runs-on: ubuntu-latest
{extra_a}    steps:
      {step_a}
  b:
    runs-on: ubuntu-latest
{extra_b}    steps:
      {step_b}
"""


def test_dedupe_ignores_jobs_that_differ_only_in_with() -> None:
    def step(version: str) -> str:
        return f"- uses: actions/setup-python@v5\n        with:\n          python-version: '{version}'"

    p, wf = profiled(_pair(step_a=step("3.11"), step_b=step("3.12")), {"a": 100, "b": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_jobs_with_different_matrix_values() -> None:
    a = "    strategy:\n      matrix:\n        py: ['3.11']\n"
    b = "    strategy:\n      matrix:\n        py: ['3.12']\n"
    p, wf = profiled(_pair(a, b), {"a (3.11)": 100, "b (3.12)": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_matches_when_only_step_names_differ() -> None:
    p, wf = profiled(
        _pair(step_a="- name: One\n        run: make", step_b="- name: Two\n        run: make"),
        {"a": 100, "b": 200},
    )
    assert len(detect_dedupe_jobs(p, wf, PATH)) == 1


def test_dedupe_group_of_three() -> None:
    text = "jobs:\n" + "".join(f"  {n}:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n" for n in "abc")
    p, wf = profiled(text, {"a": 100, "b": 300, "c": 200})
    findings = detect_dedupe_jobs(p, wf, PATH)
    assert len(findings) == 1
    assert findings[0].job == "b"
    assert findings[0].evidence["duplicate_of"] == "a, c"


DYNAMIC = "    strategy:\n      matrix:\n        py: ${{ fromJSON(needs.x.outputs.v) }}\n"


def test_dedupe_ignores_dynamic_matrix_vs_no_matrix() -> None:
    p, wf = profiled(_pair(DYNAMIC, ""), {"a": 100, "b": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_different_dynamic_matrices_on_same_key() -> None:
    other = DYNAMIC.replace("needs.x.outputs.v", "needs.y.outputs.w")
    p, wf = profiled(_pair(DYNAMIC, other), {"a": 100, "b": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_matrices_that_differ_only_in_exclude() -> None:
    def matrix(excluded: str) -> str:
        return (
            "    strategy:\n      matrix:\n        py: ['3.11', '3.12']\n"
            f"        exclude:\n          - py: '{excluded}'\n"
        )

    p, wf = profiled(_pair(matrix("3.12"), matrix("3.11")), {"a (3.11)": 100, "b (3.12)": 200})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_flags_identical_matrices_with_exclude() -> None:
    matrix = "    strategy:\n      matrix:\n        py: ['3.11', '3.12']\n        exclude:\n          - py: '3.12'\n"
    p, wf = profiled(_pair(matrix, matrix), {"a (3.11)": 100, "b (3.11)": 200})
    assert len(detect_dedupe_jobs(p, wf, PATH)) == 1


TWINS = """
jobs:
  test-a:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -q
  test-b:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -q
"""


def test_dedupe_drops_savings_under_ten_seconds() -> None:
    p, wf = profiled(TWINS, {"test-a": 3.0, "test-b": 5.0})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_drops_zero_savings() -> None:
    p, wf = profiled(TWINS, {"test-a": 30.0, "test-b": 30.0})
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_scripts_that_differ_in_internal_whitespace() -> None:
    p, wf = profiled(
        _pair(step_a="- run: grep -F 'a  b' f", step_b="- run: grep -F 'a b' f"),
        {"a": 100, "b": 200},
    )
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_dedupe_ignores_jobs_whose_step_conditions_differ() -> None:
    def steps(first: str, second: str) -> str:
        return f"- run: make lint\n        if: {first}\n      - run: make test\n        if: {second}"

    p, wf = profiled(
        _pair(
            step_a=steps("github.event_name == 'push'", "always()"),
            step_b=steps("always()", "github.event_name == 'push'"),
        ),
        {"a": 100, "b": 200},
    )
    assert detect_dedupe_jobs(p, wf, PATH) == []


def _laughs(levels: int) -> str:
    lines = ["    strategy:", "      matrix:", "        l0: &a0 [x, x, x, x, x, x, x, x, x]"]
    lines += [f"        l{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 9)}]" for i in range(1, levels)]
    return "\n".join(lines) + "\n"


def test_matrix_billion_laughs_is_cheap_and_bounded() -> None:
    start = time.perf_counter()
    wf = parse_workflow(_pair(_laughs(9), ""))
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5
    definition = wf.jobs["a"].matrix_definition
    assert definition == MATRIX_TOO_LARGE or len(definition) < 64 * 1024


def test_dedupe_skips_jobs_with_oversized_matrices() -> None:
    big = "    strategy:\n      matrix:\n        v: [" + ", ".join(str(i) for i in range(20_000)) + "]\n"
    p, wf = profiled(_pair(big, big), {"a": 100, "b": 200})
    assert wf.jobs["a"].matrix_definition == MATRIX_TOO_LARGE
    assert detect_dedupe_jobs(p, wf, PATH) == []


def test_matrix_definition_ignores_key_order() -> None:
    one = parse_workflow(_pair("    strategy:\n      matrix:\n        a: [1]\n        b: [2]\n", ""))
    two = parse_workflow(_pair("    strategy:\n      matrix:\n        b: [2]\n        a: [1]\n", ""))
    assert one.jobs["a"].matrix_definition == two.jobs["a"].matrix_definition != ""


def test_matrix_shared_scalar_is_bounded_by_expanded_size() -> None:
    big = "A" * 100_000
    refs = ", ".join(["*s"] * 2_000)
    matrix = f'    strategy:\n      matrix:\n        x: &s "{big}"\n        y: [{refs}]\n'
    start = time.perf_counter()
    wf = parse_workflow(_pair(matrix, ""))
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].matrix_definition == MATRIX_TOO_LARGE


def test_matrix_recursive_anchor_does_not_hang() -> None:
    start = time.perf_counter()
    wf = parse_workflow(_pair("    strategy:\n      matrix:\n        a: &r [*r]\n", ""))
    assert time.perf_counter() - start < 0.5
    assert wf.jobs["a"].matrix_definition == MATRIX_TOO_LARGE
