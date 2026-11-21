"""record_hypothesis fills every Hypothesis field and refuses a bottleneck that is already recorded."""

from pathlib import Path

import pytest

from pit_crew import tools
from pit_crew.contracts import Hypothesis
from pit_crew.lanes.analysis.dedupe_ledger import hypothesis_key, recorded_keys
from pit_crew.ledger import SqliteLedger

_FULL: dict[str, object] = {
    "repo": "o/r",
    "description": "pip install runs without a cache",
    "skill": "dep-cache",
    "expected_savings_seconds": 60.0,
    "workflow_path": ".github/workflows/ci.yml",
    "job": "test",
    "step": "Install dependencies",
    "bucket": "setup",
    "evidence": {"step_p50_seconds": 80.0, "max_saving_seconds": 90.0},
    "confidence": 0.8,
    "risk": "low",
    "base_sha": "abc123",
}


def _record(ledger: SqliteLedger, **overrides: object) -> str:
    return tools.call("record_hypothesis", ledger, {**_FULL, **overrides})  # type: ignore[no-any-return]


def test_record_fills_every_field(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    stored = ledger.get(Hypothesis, _record(ledger))
    assert stored.repo == "o/r"
    assert stored.description == _FULL["description"]
    assert stored.skill == "dep-cache"
    assert stored.expected_savings_seconds == 60.0
    assert stored.workflow_path == ".github/workflows/ci.yml"
    assert stored.job == "test"
    assert stored.step == "Install dependencies"
    assert stored.bucket == "setup"
    assert stored.evidence == _FULL["evidence"]
    assert stored.confidence == 0.8
    assert stored.risk == "low"
    assert stored.base_sha == "abc123"


def test_duplicate_is_rejected(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    _record(ledger)
    with pytest.raises(ValueError, match="already recorded"):
        _record(ledger, description="reworded")
    assert len(ledger.hypotheses_for_repo("o/r")) == 1


def test_same_job_different_skill_is_allowed(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    _record(ledger)
    _record(ledger, skill="affected-tests")
    assert len(ledger.hypotheses_for_repo("o/r")) == 2


def test_savings_above_max_saving_is_rejected(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    with pytest.raises(ValueError, match="max_saving_seconds"):
        _record(ledger, expected_savings_seconds=60.0, evidence={"max_saving_seconds": 50.0})
    assert ledger.hypotheses_for_repo("o/r") == []


def test_bad_workflow_path_is_rejected(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    with pytest.raises(ValueError, match="workflow_path"):
        _record(ledger, workflow_path="../ci.yml")


def test_non_positive_savings_is_rejected(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    with pytest.raises(ValueError, match="expected_savings_seconds"):
        _record(ledger, expected_savings_seconds=0.0)


def test_old_four_argument_call_still_works(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    hypothesis_id = tools.call(
        "record_hypothesis",
        ledger,
        {"repo": "o/r", "description": "no cache", "skill": "dep-cache", "expected_savings_seconds": 120},
    )
    assert ledger.get(Hypothesis, hypothesis_id).workflow_path == ""


def test_keys_cover_every_recorded_hypothesis(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "l.db")
    _record(ledger)
    assert recorded_keys(ledger, "o/r") == {
        hypothesis_key("dep-cache", ".github/workflows/ci.yml", "test", "Install dependencies")
    }
    assert recorded_keys(ledger, "other/repo") == set()
