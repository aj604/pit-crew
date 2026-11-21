import gzip
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pit_crew import tools
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.detectors import DETECTORS
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
        "event": "pull_request",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "conclusion": "success",
        "run_attempt": 1,
        "run_started_at": at(0),
        "jobs": [job("test", 370, steps)],
    }


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(WORKFLOW)
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    raw = [run(i) for i in range(1, 6)]
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)
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
    assert set(result["profile_summary"]) == {
        "runs_analyzed",
        "wall_clock_median_seconds",
        "wall_clock_p95_seconds",
        "critical_path",
        "critical_path_buckets",
        "biggest_bucket",
        "biggest_bucket_p95",
    }


@pytest.mark.usefixtures("synthetic")
def test_find_bottlenecks_hides_recorded_findings() -> None:
    ledger = InMemoryLedger()
    shard = find(ledger)["findings"][0]
    tools.call("record_hypothesis", ledger, {"repo": REPO, **shard})

    assert [f["skill"] for f in find(ledger)["findings"]] == ["dep-cache"]


def test_find_bottlenecks_reports_waiting_as_uncovered(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text((FIXTURES / "fastapi" / "test.yml").read_text())
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        raw = json.load(f)
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)

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
