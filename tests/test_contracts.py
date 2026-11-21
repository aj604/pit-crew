import pytest

from pit_crew.contracts import BUCKETS, PLAYBOOK_SKILLS, Hypothesis, InvalidHypothesisError
from pit_crew.ledger import InMemoryLedger, Ledger, SqliteLedger


def full_hypothesis() -> Hypothesis:
    return Hypothesis(
        repo="org/repo",
        description="npm ci misses the cache on the critical path",
        skill="dep-cache",
        expected_savings_seconds=95.0,
        base_sha="abc123",
        workflow_path=".github/workflows/ci.yml",
        job="test",
        step="Run npm ci",
        bucket="setup",
        evidence={"step_p50_seconds": 104.0, "max_saving_seconds": 310.5, "runs_sampled": 146, "straggler": "macos"},
        confidence=0.8,
        risk="low",
    )


def test_old_positional_callers_still_work() -> None:
    h = Hypothesis("org/repo", "no cache", "dep-cache", 120)
    assert (h.job, h.step, h.bucket, h.evidence, h.confidence, h.risk) == ("", "", "", {}, None, "")


@pytest.mark.parametrize("ledger_kind", ["memory", "sqlite"])
def test_new_fields_round_trip_through_the_ledger(ledger_kind: str, tmp_path: object) -> None:
    ledger: Ledger = InMemoryLedger() if ledger_kind == "memory" else SqliteLedger(f"{tmp_path}/pit_crew.db")
    h = full_hypothesis()
    ledger.add(h)
    assert ledger.get(Hypothesis, h.id) == h


def test_every_playbook_skill_and_bucket_is_accepted() -> None:
    for skill in PLAYBOOK_SKILLS:
        for bucket in BUCKETS:
            Hypothesis("o/r", "d", skill, 1.0, bucket=bucket)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"skill": "cache-stuff"}, "skill"),
        ({"skill": "flake-quarantine"}, "skill"),  # retries hide failures; not a playbook skill
        ({"bucket": "queue"}, "bucket"),
        ({"risk": "none"}, "risk"),
        ({"confidence": 1.5}, "confidence"),
        ({"confidence": -0.1}, "confidence"),
    ],
)
def test_values_outside_the_vocabularies_are_rejected(overrides: dict[str, object], message: str) -> None:
    args: dict[str, object] = {"repo": "o/r", "description": "d", "skill": "dep-cache", "expected_savings_seconds": 1.0}
    with pytest.raises(InvalidHypothesisError, match=message):
        Hypothesis(**(args | overrides))  # type: ignore[arg-type]
