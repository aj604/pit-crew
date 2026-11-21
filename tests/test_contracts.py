import json
import sqlite3
from pathlib import Path

import pytest

from pit_crew.contracts import (
    BUCKETS,
    PLAYBOOK_SKILLS,
    BugCase,
    Hypothesis,
    InvalidBugCaseError,
    InvalidHypothesisError,
    ReplayPlan,
)
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


def test_bug_case_old_positional_callers_still_work() -> None:
    commit = BugCase("commit", "historical red commit", "abc123")
    mutation = BugCase("mutation", "synthetic mutation", None, {"src/app.py": "1/0\n"}, "trace.zip")
    inferred = ReplayPlan("cand-1", ("abc123",))
    assert commit.expected_checks == ()
    assert mutation.expected_checks == ()
    assert inferred.cases[0].expected_checks == ()


@pytest.mark.parametrize("ledger_kind", ["memory", "sqlite"])
def test_bug_case_expected_checks_round_trip_through_the_ledger(ledger_kind: str, tmp_path: Path) -> None:
    ledger: Ledger = InMemoryLedger() if ledger_kind == "memory" else SqliteLedger(tmp_path / "pit_crew.db")
    plan = ReplayPlan(
        candidate_id="cand-1",
        cases=(
            BugCase(
                kind="commit",
                commit_sha="b2b2b2b",
                reason="unit test failure",
                expected_checks=("CI/Unit tests", "CI/test"),
            ),
            BugCase(
                kind="mutation",
                mutated_files={"src/merge.ts": "return a != b;\n"},
                reason="lint: == -> !=",
                expected_checks=("CI/lint",),
            ),
        ),
    )
    ledger.add(plan)
    loaded = ledger.get(ReplayPlan, plan.id)
    assert loaded == plan
    assert loaded.cases[0].expected_checks == ("CI/Unit tests", "CI/test")
    assert loaded.cases[1].expected_checks == ("CI/lint",)


def test_sqlite_ledger_loads_legacy_bug_case_without_expected_checks(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    ledger = SqliteLedger(db_path)
    legacy_payload = {
        "id": "plan-legacy",
        "candidate_id": "cand-1",
        "bug_shas": ["abc123"],
        "cases": [
            {
                "kind": "commit",
                "reason": "historical red commit",
                "commit_sha": "abc123",
                "mutated_files": {},
                "artifact_ref": None,
            }
        ],
    }
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO records (kind, record_id, payload) VALUES (?, ?, ?)",
                ("ReplayPlan", "plan-legacy", json.dumps(legacy_payload)),
            )
    finally:
        conn.close()

    loaded = ledger.get(ReplayPlan, "plan-legacy")
    assert loaded.cases[0].commit_sha == "abc123"
    assert loaded.cases[0].expected_checks == ()


def test_bug_case_normalizes_and_dedupes_expected_checks() -> None:
    case = BugCase(
        kind="commit",
        commit_sha="b2b2b2b",
        expected_checks=(" CI/Unit tests ", "CI/test", "CI/Unit tests"),
    )
    assert case.expected_checks == ("CI/Unit tests", "CI/test")


@pytest.mark.parametrize(
    "bad_checks",
    [
        "CI/lint",
        ["CI/lint"],
        ("",),
        ("   ",),
        ("CI/lint", ""),
        (123,),
    ],
)
def test_bug_case_rejects_invalid_expected_checks(bad_checks: object) -> None:
    with pytest.raises(InvalidBugCaseError, match="expected_checks"):
        BugCase(kind="commit", commit_sha="abc123", expected_checks=bad_checks)  # type: ignore[arg-type]
