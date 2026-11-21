import gzip
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pit_crew.lanes.analysis.critical_path import CIProfile, profile
from pit_crew.lanes.analysis.detectors.dedupe import detect_dedupe_jobs
from pit_crew.lanes.analysis.history import parse_runs
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow

FIXTURES = Path(__file__).parent / "fixtures"
PATH = ".github/workflows/ci.yml"
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(name: str, seconds: float) -> dict[str, Any]:
    return {
        "id": hash(name),
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
        "jobs": [job(n, s) for n, s in durations.items()],
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
        run: pytest   -q
  test-b:
    runs-on: ubuntu-latest
    steps:
      - name: Other name
        run: pytest -q
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
