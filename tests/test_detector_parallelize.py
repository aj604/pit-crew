import gzip
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pit_crew.lanes.analysis.critical_path import CIProfile, CriticalPath, profile
from pit_crew.lanes.analysis.detectors.parallelize import detect_parallelize_dag
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
PATH = ".github/workflows/ci.yml"
SPANS = {"lint": (0, 60), "test": (60, 360)}


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(name: str, start: float, end: float) -> dict[str, Any]:
    return {
        "id": hash(name),
        "name": name,
        "run_attempt": 1,
        "conclusion": "success",
        "created_at": at(start),
        "started_at": at(start),
        "completed_at": at(end),
        "labels": [],
        "steps": [],
    }


def profiled(yaml_text: str, spans: Mapping[str, tuple[float, float]]) -> tuple[CIProfile, Workflow]:
    wf = parse_workflow(yaml_text)
    raw = [
        {
            "id": i,
            "path": PATH,
            "event": "push",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "conclusion": "success",
            "run_attempt": 1,
            "run_started_at": at(0),
            "jobs": [job(n, s, e) for n, (s, e) in spans.items()],
        }
        for i in (1, 2, 3)
    ]
    return profile(wf, parse_runs(raw)), wf


def two_jobs(test_body: str, lint_body: str = "") -> str:
    return f"""
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
{lint_body}
  test:
    needs: lint
    runs-on: ubuntu-latest
{test_body}
"""


def test_parallelize_flags_lint_before_test() -> None:
    p, wf = profiled(two_jobs("    steps:\n      - run: pytest"), SPANS)
    [f] = detect_parallelize_dag(p, wf, PATH)
    assert f.skill == "parallelize-dag"
    assert f.job == "test"
    assert f.step == ""
    assert f.bucket == ""
    assert f.risk == "medium"
    assert f.confidence == 0.6
    assert f.expected_savings_seconds == 60.0
    assert f.evidence == {
        "removed_need": "lint",
        "critical_path_seconds": 360.0,
        "critical_path_after_seconds": 300.0,
    }


def test_parallelize_keeps_edges_that_pass_outputs() -> None:
    body = '    steps:\n      - run: echo "${{ needs.lint.outputs.x }}"'
    p, wf = profiled(two_jobs(body), SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_keeps_artifact_edges() -> None:
    lint = "    steps:\n      - uses: actions/upload-artifact@v4"
    test = "    steps:\n      - uses: actions/download-artifact@v4"
    p, wf = profiled(two_jobs(test, lint), SPANS)
    assert detect_parallelize_dag(p, wf, PATH) == []


def test_parallelize_keeps_gate_jobs() -> None:
    wf_yaml = """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
  b:
    needs: a
    runs-on: ubuntu-latest
  gate:
    needs: [a, b]
    runs-on: ubuntu-latest
    steps:
      - run: echo '${{ toJSON(needs) }}'
"""
    p, wf = profiled(wf_yaml, {"a": (0, 50), "b": (50, 100), "gate": (100, 110)})
    assert [f.job for f in detect_parallelize_dag(p, wf, PATH)] == ["b"]  # the gate's own edges stay


def test_parallelize_ignores_edges_off_the_critical_path() -> None:
    p, wf = profiled(two_jobs("    steps:\n      - run: pytest"), SPANS)
    off_path = replace(p, critical_path=CriticalPath(0.0, ()))
    assert detect_parallelize_dag(off_path, wf, PATH) == []


def test_parallelize_empty_profile() -> None:
    wf = parse_workflow(two_jobs("    steps:\n      - run: pytest"))
    assert detect_parallelize_dag(profile(wf, []), wf, PATH) == []


def test_parallelize_fastapi() -> None:
    wf = parse_workflow((FIXTURES / "fastapi" / "test.yml").read_text())
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        runs = parse_runs(json.load(f))
    findings = detect_parallelize_dag(profile(wf, runs), wf, "test.yml")
    assert all(f.job != "test-alls-green" for f in findings)
    assert all(f.expected_savings_seconds > 0 for f in findings)
