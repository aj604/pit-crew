from pathlib import Path

import pytest

from pit_crew.contracts import (
    BugCase,
    Candidate,
    Evidence,
    Hypothesis,
    ReplayPlan,
    Verdict,
    is_allowed_candidate_path,
)
from pit_crew.ledger import InMemoryLedger, Ledger, SqliteLedger


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> Ledger:
    if request.param == "memory":
        return InMemoryLedger()
    return SqliteLedger(tmp_path / "pit_crew.db")


def test_ledger_round_trips_all_v1_records_and_queries_history(ledger: Ledger) -> None:
    h1 = Hypothesis(
        repo="org/repo",
        description="installs miss uv cache",
        skill="dep-cache",
        expected_savings_seconds=80.0,
        base_sha="sha0",
        workflow_path=".github/workflows/ci.yml",
    )
    h2 = Hypothesis(
        repo="org/repo",
        description="unsharded e2e suite",
        skill="shard-by-timing",
        expected_savings_seconds=110.0,
        base_sha="sha1",
    )
    other_repo_cand = Candidate(
        hypothesis_id="other-h",
        branch="pc/other",
        summary="other repo",
        repo="other/repo",
    )
    ledger.add(h1)
    ledger.add(h2)
    ledger.add(other_repo_cand)

    c1 = Candidate(
        hypothesis_id=h1.id,
        branch="pc/h1-dep-cache",
        summary="enable uv cache",
        repo="org/repo",
        base_sha="sha0",
        head_sha="sha1",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    assert c1.changed_files == (".github/workflows/ci.yml",)
    ledger.add(c1)

    plan = ReplayPlan(
        candidate_id=c1.id,
        cases=(
            BugCase(kind="commit", commit_sha="bad111", reason="missing css asset", artifact_ref="fail.png"),
            BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="smoke test failure"),
        ),
    )
    assert plan.bug_shas == ("bad111",)
    ledger.add(plan)

    ev = Evidence(
        candidate_id=c1.id,
        baseline_seconds=(180.0, 185.0, 178.0, 182.0, 181.0),
        candidate_seconds=(55.0, 52.0, 54.0, 58.0, 53.0),
        bugs_total=2,
        bugs_caught=2,
        run_urls=("https://github.com/org/repo/actions/runs/1",),
        bug_outcomes=({"kind": "commit", "caught": True}, {"kind": "mutation", "caught": True}),
    )
    ledger.add(ev)

    v1 = Verdict(
        candidate_id=c1.id,
        keep=True,
        speedup=3.35,
        p_value=0.004,
        reason="3.35x faster, caught 2/2 bugs (SLA met)",
        median_seconds=54.0,
        p95_seconds=57.4,
        sla_met=True,
    )
    ledger.add(v1)

    loaded_ev = ledger.get(Evidence, ev.id)
    assert loaded_ev.run_urls == ("https://github.com/org/repo/actions/runs/1",)
    loaded_plan = ledger.get(ReplayPlan, plan.id)
    assert loaded_plan.cases[0].artifact_ref == "fail.png"
    assert loaded_plan.cases[1].mutated_files == {"src/app.py": "1/0\n"}

    with pytest.raises(KeyError):
        ledger.get(Hypothesis, "missing-id")

    attempts = ledger.prior_attempts("org/repo")
    assert len(attempts) == 2
    assert attempts[0]["keep"] is True
    assert attempts[0]["sla_met"] is True
    assert attempts[1]["hypothesis_id"] == h2.id
    assert attempts[1]["candidate_id"] is None
    assert ledger.latest_kept_candidate("org/repo") == c1


def test_candidate_path_allowlist_blocks_source_and_tests_while_allowing_workflows_and_runner_configs() -> None:
    assert is_allowed_candidate_path(".github/workflows/ci.yml")
    assert is_allowed_candidate_path(".github/actions/setup/action.yml")
    assert is_allowed_candidate_path("playwright.config.ts")
    assert is_allowed_candidate_path("pyproject.toml")
    assert is_allowed_candidate_path("packages/web/vitest.config.ts")

    assert not is_allowed_candidate_path("")
    assert not is_allowed_candidate_path(".github")
    assert not is_allowed_candidate_path("src/pit_crew/rater.py")
    assert not is_allowed_candidate_path("tests/test_app.py")
    assert not is_allowed_candidate_path("e2e/playwright.config.ts")
    assert not is_allowed_candidate_path("../.github/workflows/ci.yml")
    assert not is_allowed_candidate_path("/etc/passwd")
    assert not is_allowed_candidate_path("README.md")


def test_bug_case_validates_required_fields() -> None:
    with pytest.raises(ValueError, match="commit_sha"):
        BugCase(kind="commit")
    with pytest.raises(ValueError, match="mutated_files"):
        BugCase(kind="mutation")
    with pytest.raises(ValueError, match=r"BugCase\.kind"):
        BugCase(kind="unknown", commit_sha="abc")
