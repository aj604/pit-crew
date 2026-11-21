import gzip
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pit_crew import tools
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis import tools as analysis_tools
from pit_crew.lanes.analysis.detectors import DETECTORS
from pit_crew.lanes.analysis.detectors.base import Finding
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
REPO = "acme/app"

Raw = dict[str, Any]

WORKFLOW = """
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: npm ci
      - name: Run tests
        run: npm test
"""


def at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def job(name: str, completed: float, steps: Sequence[tuple[str, float]]) -> Raw:
    return {
        "id": 1,
        "name": name,
        "run_attempt": 1,
        "conclusion": "success",
        "created_at": at(0),
        "started_at": at(0),
        "completed_at": at(completed),
        "labels": [],
        "steps": [
            {"number": i, "name": n, "conclusion": "success", "started_at": at(0), "completed_at": at(s)}
            for i, (n, s) in enumerate(steps, 1)
        ],
    }


def run(run_id: int) -> Raw:
    steps = [("Install", 120.0), ("Run tests", 240.0)]
    return {
        "id": run_id,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": at(0),
        "jobs": [job("test", 370, steps)],
    }


def same_revision(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """Make every run in the patched history count as having run the checkout's workflow file."""
    blob = history.workflow_blob_sha(text)
    monkeypatch.setattr(history, "fetch_workflow_blob_shas", lambda _r, _p, refs, **_k: dict.fromkeys(refs, blob))


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    raw = [run(i) for i in range(1, 6)]
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    same_revision(monkeypatch, WORKFLOW)
    build_request("analysis", "")


def find(ledger: InMemoryLedger) -> dict[str, Any]:
    result: dict[str, Any] = tools.call("find_bottlenecks", ledger, {"repo": REPO, "workflow": "ci.yml"})
    return result


@pytest.mark.usefixtures("synthetic")
def test_find_bottlenecks_ranks_findings() -> None:
    result = find(InMemoryLedger())

    json.dumps(result)
    assert [f["skill"] for f in result["findings"]] == ["shard-by-timing", "dep-cache"]
    savings = [f["expected_savings_seconds"] for f in result["findings"]]
    assert savings == sorted(savings, reverse=True)
    assert result["profile_summary"]["runs_analyzed"] == 5
    assert result["profile_summary"]["base_sha"] == "a" * 40
    assert set(result["profile_summary"]) == {
        "base_sha",
        "runs_analyzed",
        "wall_clock_median_seconds",
        "wall_clock_p95_seconds",
        "critical_path",
        "critical_path_buckets",
        "biggest_bucket",
        "biggest_bucket_p95",
        "runs_excluded_revision",
        "runs_excluded_event",
    }


@pytest.mark.usefixtures("synthetic")
def test_find_bottlenecks_hides_recorded_findings() -> None:
    ledger = InMemoryLedger()
    shard = find(ledger)["findings"][0]
    tools.call("record_hypothesis", ledger, {"repo": REPO, **shard})

    assert [f["skill"] for f in find(ledger)["findings"]] == ["dep-cache"]


TWO_INSTALLS = """
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Install dependencies
        run: cd frontend && npm ci
      - name: Install dependencies
        run: cd dashboard && npm ci
"""


def test_recording_the_second_same_named_step_hides_only_that_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(TWO_INSTALLS)
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    steps = [("Install dependencies", 120.0), ("Install dependencies", 60.0)]
    raw = [{**run(i), "jobs": [job("test", 200, steps)]} for i in range(1, 6)]
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    same_revision(monkeypatch, TWO_INSTALLS)
    build_request("analysis", "")
    ledger = InMemoryLedger()

    def installs() -> list[dict[str, Any]]:
        return [f for f in find(ledger)["findings"] if f["skill"] == "dep-cache"]

    assert [f["step"] for f in sorted(installs(), key=lambda f: f["step"])] == [
        "Install dependencies",
        "Install dependencies [2]",
    ]
    second = next(f for f in installs() if f["step"] == "Install dependencies [2]")
    tools.call("record_hypothesis", ledger, {"repo": REPO, **second})

    assert [f["step"] for f in installs()] == ["Install dependencies"]


def test_find_bottlenecks_reports_waiting_as_uncovered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text((FIXTURES / "fastapi" / "test.yml").read_text())
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        raw = json.load(f)
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
    same_revision(monkeypatch, (FIXTURES / "fastapi" / "test.yml").read_text())

    build_request("analysis", "")
    result = tools.call("find_bottlenecks", InMemoryLedger(), {"repo": "fastapi/fastapi", "workflow": "test.yml"})

    assert result["profile_summary"]["biggest_bucket_p95"] == "waiting"
    assert any("waiting" in note for note in result["uncovered"])


@pytest.mark.usefixtures("synthetic")
def test_find_bottlenecks_has_nothing_uncovered_when_running_dominates() -> None:
    assert find(InMemoryLedger())["uncovered"] == []


def test_every_detector_is_registered() -> None:
    assert {d.__name__ for d in DETECTORS} == {
        "detect_dep_cache",
        "detect_shard_by_timing",
        "detect_parallelize_dag",
        "detect_dedupe_jobs",
    }


def fake_finding(seconds: float, job_id: str) -> Finding:
    return Finding(
        skill="dep-cache",
        workflow_path=".github/workflows/ci.yml",
        job=job_id,
        step="",
        bucket="setup",
        description="x",
        expected_savings_seconds=seconds,
        evidence={"max_saving_seconds": 100.0},
        confidence=0.5,
        risk="low",
    )


@pytest.mark.usefixtures("synthetic")
def test_find_bottlenecks_drops_findings_under_ten_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    small, zero, big = fake_finding(5.0, "a"), fake_finding(0.0, "b"), fake_finding(40.0, "c")
    monkeypatch.setattr(analysis_tools, "DETECTORS", (lambda *_args: [small, zero, big],))

    assert [f["job"] for f in find(InMemoryLedger())["findings"]] == ["c"]


@pytest.mark.usefixtures("synthetic")
def test_find_bottlenecks_with_no_analyzed_runs_says_nothing_was_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: [])

    result = find(InMemoryLedger())

    assert len(result["uncovered"]) == 1
    assert "No push, workflow_dispatch, schedule or merge_group runs were found" in result["uncovered"][0]
    assert result["profile_summary"]["base_sha"] == ""
    assert result["findings"] == []
