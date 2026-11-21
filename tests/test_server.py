import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pit_crew.contracts import BugCase, Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.ledger import InMemoryLedger, Ledger, SqliteLedger
from pit_crew.server import create_app


class _StubDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.fail = fail

    def run_cycle(self, repo: str) -> None:
        self.calls.append(("run_cycle", repo))
        self.started.set()
        self.release.wait(timeout=5)
        try:
            if self.fail:
                msg = "boom"
                raise RuntimeError(msg)
        finally:
            self.finished.set()

    def run_until_sla(self, repo: str) -> None:
        self.calls.append(("run_until_sla", repo))
        self.started.set()
        self.release.wait(timeout=5)
        try:
            if self.fail:
                msg = "boom"
                raise RuntimeError(msg)
        finally:
            self.finished.set()


class _AsyncStubDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.done = threading.Event()

    async def run_cycle(self, repo: str) -> None:
        self.calls.append(("run_cycle", repo))
        self.done.set()

    async def run_until_sla(self, repo: str) -> None:
        self.calls.append(("run_until_sla", repo))
        self.done.set()


def _make_ledger(backend: str, tmp_path: Path) -> Ledger:
    if backend == "sqlite":
        return SqliteLedger(tmp_path / "ledger.db")
    return InMemoryLedger()


def test_health_in_memory_ledger() -> None:
    app = create_app(ledger=InMemoryLedger(), dispatcher=_StubDispatcher(), webhook_secret="test-secret")  # noqa: S106
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ledger": "memory"}


def test_health_sqlite_ledger(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ledger": "sqlite"}


def test_trigger_cycle_default_dispatches_run_cycle() -> None:
    dispatcher = _StubDispatcher()
    dispatcher.release.set()
    app = create_app(ledger=InMemoryLedger(), dispatcher=dispatcher)

    with TestClient(app) as client:
        response = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert dispatcher.finished.wait(timeout=5)

    assert response.status_code == 202
    assert response.json() == {"status": "started", "repo": "acme/widget"}
    assert dispatcher.calls == [("run_cycle", "acme/widget")]


def test_trigger_cycle_until_sla_dispatches_run_until_sla() -> None:
    dispatcher = _AsyncStubDispatcher()
    app = create_app(ledger=InMemoryLedger(), dispatcher=dispatcher)

    with TestClient(app) as client:
        response = client.post("/triggers/cycle", json={"repo": "acme/widget", "until_sla": True})
        assert dispatcher.done.wait(timeout=5)

    assert response.status_code == 202
    assert response.json() == {"status": "started", "repo": "acme/widget"}
    assert dispatcher.calls == [("run_until_sla", "acme/widget")]


def test_trigger_cycle_conflict_and_lock_release() -> None:
    dispatcher = _StubDispatcher()
    app = create_app(ledger=InMemoryLedger(), dispatcher=dispatcher)

    with TestClient(app) as client:
        first = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert first.status_code == 202
        assert dispatcher.started.wait(timeout=5)
        assert "acme/widget" in app.state.active_repos

        conflict = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert conflict.status_code == 409

        dispatcher.release.set()
        assert dispatcher.finished.wait(timeout=5)

        # Wait briefly for the asyncio task's finally block to discard the repo lock.
        while "acme/widget" in app.state.active_repos:
            client.get("/health")

        dispatcher.finished.clear()
        second = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert second.status_code == 202
        assert dispatcher.finished.wait(timeout=5)


def test_trigger_cycle_releases_lock_on_exception() -> None:
    dispatcher = _StubDispatcher(fail=True)
    dispatcher.release.set()
    app = create_app(ledger=InMemoryLedger(), dispatcher=dispatcher)

    with TestClient(app) as client:
        first = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert first.status_code == 202
        assert dispatcher.finished.wait(timeout=5)

        while "acme/widget" in app.state.active_repos:
            client.get("/health")

        dispatcher.fail = False
        dispatcher.finished.clear()
        second = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert second.status_code == 202
        assert dispatcher.finished.wait(timeout=5)


def test_trigger_cycle_without_dispatcher_returns_503() -> None:
    app = create_app(ledger=InMemoryLedger())
    with TestClient(app) as client:
        response = client.post("/triggers/cycle", json={"repo": "acme/widget"})

    assert response.status_code == 503


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_repo_attempts_empty(backend: str, tmp_path: Path) -> None:
    ledger = _make_ledger(backend, tmp_path)
    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        response = client.get("/api/repos/acme/widget/attempts")

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_repo_attempts_returns_prior_attempts(backend: str, tmp_path: Path) -> None:
    ledger = _make_ledger(backend, tmp_path)
    hyp_rated = Hypothesis(
        id="h1",
        repo="acme/widget",
        description="Cache pip dependencies",
        skill="dep-cache",
        expected_savings_seconds=30.0,
        base_sha="abc123",
    )
    cand = Candidate(
        id="c1",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/dep-cache",
        base_sha="abc123",
        head_sha="def456",
        summary="Added actions/cache",
        changed_files=(".github/workflows/ci.yml",),
    )
    verdict = Verdict(
        id="v1",
        candidate_id="c1",
        keep=True,
        speedup=1.35,
        p_value=0.01,
        median_seconds=65.0,
        p95_seconds=72.0,
        sla_met=True,
        reason="statistically significant speedup",
    )
    hyp_unattempted = Hypothesis(
        id="h2",
        repo="acme/widget",
        description="Shard slow test suite",
        skill="shard-by-timing",
        expected_savings_seconds=45.0,
        base_sha="def456",
    )
    other_repo_hyp = Hypothesis(
        id="h3",
        repo="other/repo",
        description="Unrelated repo hypothesis",
        skill="dedupe-jobs",
        expected_savings_seconds=10.0,
    )
    for record in (hyp_rated, cand, verdict, hyp_unattempted, other_repo_hyp):
        ledger.add(record)

    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        response = client.get("/api/repos/acme/widget/attempts")

    assert response.status_code == 200
    assert response.json() == ledger.prior_attempts("acme/widget")
    assert len(response.json()) == 2
    assert response.json()[0]["candidate_id"] == "c1"
    assert response.json()[0]["keep"] is True
    assert response.json()[1]["hypothesis_id"] == "h2"
    assert response.json()[1]["candidate_id"] is None


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_repo_records_empty(backend: str, tmp_path: Path) -> None:
    ledger = _make_ledger(backend, tmp_path)
    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        response = client.get("/api/repos/acme/widget/records")

    assert response.status_code == 200
    assert response.json() == {
        "hypotheses": [],
        "candidates": [],
        "replay_plans": [],
        "evidence": [],
        "verdicts": [],
    }


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_repo_records_scoped_to_repo(backend: str, tmp_path: Path) -> None:
    ledger = _make_ledger(backend, tmp_path)
    hyp = Hypothesis(
        id="h1",
        repo="acme/widget",
        description="Cache pip dependencies",
        skill="dep-cache",
        expected_savings_seconds=30.0,
        base_sha="abc123",
    )
    cand = Candidate(
        id="c1",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/dep-cache",
        base_sha="abc123",
        head_sha="def456",
        summary="Added actions/cache",
        changed_files=(".github/workflows/ci.yml",),
    )
    plan = ReplayPlan(
        id="rp1",
        candidate_id="c1",
        cases=(BugCase(kind="commit", commit_sha="bad111", reason="regression"),),
    )
    ev = Evidence(
        id="e1",
        candidate_id="c1",
        baseline_seconds=(90.0, 92.0),
        candidate_seconds=(60.0, 62.0),
        bugs_total=1,
        bugs_caught=1,
        run_urls=("https://github.com/acme/widget/actions/runs/1",),
        bug_outcomes=({"commit_sha": "bad111", "caught": True},),
    )
    verdict = Verdict(
        id="v1",
        candidate_id="c1",
        keep=True,
        speedup=1.5,
        p_value=0.01,
        median_seconds=61.0,
        p95_seconds=62.0,
        sla_met=True,
        reason="faster and caught all bugs",
    )

    other_hyp = Hypothesis(
        id="h2",
        repo="other/repo",
        description="Unrelated repo hypothesis",
        skill="dedupe-jobs",
        expected_savings_seconds=10.0,
    )
    other_cand = Candidate(
        id="c2",
        hypothesis_id="h2",
        repo="other/repo",
        branch="pit-crew/dedupe-jobs",
        summary="Removed duplicate job",
    )
    other_plan = ReplayPlan(id="rp2", candidate_id="c2", bug_shas=("deadbeef",))
    other_ev = Evidence(
        id="e2",
        candidate_id="c2",
        baseline_seconds=(50.0,),
        candidate_seconds=(40.0,),
        bugs_total=1,
        bugs_caught=1,
    )
    other_verdict = Verdict(
        id="v2",
        candidate_id="c2",
        keep=True,
        speedup=1.25,
        p_value=0.05,
        reason="faster",
    )

    for record in (hyp, cand, plan, ev, verdict, other_hyp, other_cand, other_plan, other_ev, other_verdict):
        ledger.add(record)

    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        response = client.get("/api/repos/acme/widget/records")

    assert response.status_code == 200
    body = response.json()
    assert list(body.keys()) == ["hypotheses", "candidates", "replay_plans", "evidence", "verdicts"]
    assert [h["id"] for h in body["hypotheses"]] == ["h1"]
    assert [c["id"] for c in body["candidates"]] == ["c1"]
    assert body["candidates"][0]["changed_files"] == [".github/workflows/ci.yml"]
    assert [p["id"] for p in body["replay_plans"]] == ["rp1"]
    assert body["replay_plans"][0]["bug_shas"] == ["bad111"]
    assert [e["id"] for e in body["evidence"]] == ["e1"]
    assert body["evidence"][0]["baseline_seconds"] == [90.0, 92.0]
    assert body["evidence"][0]["bugs_caught"] == 1
    assert [v["id"] for v in body["verdicts"]] == ["v1"]
    assert body["verdicts"][0]["keep"] is True
