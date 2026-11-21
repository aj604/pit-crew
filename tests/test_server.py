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


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_repo_status_idle_empty(backend: str, tmp_path: Path) -> None:
    ledger = _make_ledger(backend, tmp_path)
    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        response = client.get("/api/repos/acme/widget/status")

    assert response.status_code == 200
    assert response.json() == {
        "repo": "acme/widget",
        "active": False,
        "base_sha": None,
        "latest_verdict": None,
        "sla_met": False,
    }


def test_repo_status_reflects_active_and_idle_cycle() -> None:
    dispatcher = _StubDispatcher()
    app = create_app(ledger=InMemoryLedger(), dispatcher=dispatcher)

    with TestClient(app) as client:
        idle_before = client.get("/api/repos/acme/widget/status")
        assert idle_before.status_code == 200
        assert idle_before.json()["active"] is False

        trigger = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert trigger.status_code == 202
        assert dispatcher.started.wait(timeout=5)

        active = client.get("/api/repos/acme/widget/status")
        assert active.status_code == 200
        assert active.json()["active"] is True

        other = client.get("/api/repos/other/repo/status")
        assert other.status_code == 200
        assert other.json()["active"] is False

        dispatcher.release.set()
        assert dispatcher.finished.wait(timeout=5)
        while "acme/widget" in app.state.active_repos:
            client.get("/health")

        idle_after = client.get("/api/repos/acme/widget/status")
        assert idle_after.status_code == 200
        assert idle_after.json()["active"] is False


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_repo_status_post_verdict_and_base_sha_progression(backend: str, tmp_path: Path) -> None:
    ledger = _make_ledger(backend, tmp_path)
    app = create_app(ledger=ledger)

    # 1. Hypothesis only: base_sha falls back to hypothesis.base_sha, no verdict yet.
    hyp1 = Hypothesis(
        id="h1",
        repo="acme/widget",
        description="Cache pip dependencies",
        skill="dep-cache",
        expected_savings_seconds=30.0,
        base_sha="sha-base-1",
    )
    ledger.add(hyp1)
    with TestClient(app) as client:
        resp_hyp = client.get("/api/repos/acme/widget/status")
    assert resp_hyp.status_code == 200
    assert resp_hyp.json()["base_sha"] == "sha-base-1"
    assert resp_hyp.json()["latest_verdict"] is None
    assert resp_hyp.json()["sla_met"] is False

    # 2. Rejected candidate: base_sha stays at candidate.base_sha, latest_verdict populated, sla_met=False.
    cand1 = Candidate(
        id="c1",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/dep-cache",
        base_sha="sha-base-1",
        head_sha="sha-rejected",
        summary="Tried cache",
    )
    verdict1 = Verdict(
        id="v1",
        candidate_id="c1",
        keep=False,
        speedup=1.02,
        p_value=0.3,
        median_seconds=95.0,
        p95_seconds=140.0,
        sla_met=False,
        reason="speedup is within noise",
    )
    ledger.add(cand1)
    ledger.add(verdict1)
    with TestClient(app) as client:
        resp_rejected = client.get("/api/repos/acme/widget/status")
    assert resp_rejected.status_code == 200
    body_rejected = resp_rejected.json()
    assert body_rejected["base_sha"] == "sha-base-1"
    assert body_rejected["sla_met"] is False
    assert body_rejected["latest_verdict"]["id"] == "v1"
    assert body_rejected["latest_verdict"]["keep"] is False

    # 3. Kept candidate meeting 60s/120s SLA: base_sha advances to head_sha, sla_met=True.
    #    Also add an unrelated repo's verdict to verify isolation.
    hyp2 = Hypothesis(
        id="h2",
        repo="acme/widget",
        description="Shard slow test suite",
        skill="shard-by-timing",
        expected_savings_seconds=50.0,
        base_sha="sha-base-1",
    )
    cand2 = Candidate(
        id="c2",
        hypothesis_id="h2",
        repo="acme/widget",
        branch="pit-crew/shard",
        base_sha="sha-base-1",
        head_sha="sha-kept-2",
        summary="Sharded tests",
    )
    verdict2 = Verdict(
        id="v2",
        candidate_id="c2",
        keep=True,
        speedup=2.1,
        p_value=0.004,
        median_seconds=45.0,
        p95_seconds=80.0,
        sla_met=True,
        reason="2.10x faster, caught 3/3 bugs (SLA met)",
    )
    other_cand = Candidate(
        id="c-other",
        hypothesis_id="h-other",
        repo="other/repo",
        branch="pit-crew/other",
        base_sha="other-base",
        head_sha="other-head",
        summary="Other repo",
    )
    other_verdict = Verdict(
        id="v-other",
        candidate_id="c-other",
        keep=False,
        speedup=1.0,
        p_value=0.5,
        median_seconds=200.0,
        p95_seconds=300.0,
        sla_met=False,
        reason="noise",
    )
    for rec in (hyp2, cand2, verdict2, other_cand, other_verdict):
        ledger.add(rec)

    with TestClient(app) as client:
        resp_kept = client.get("/api/repos/acme/widget/status")
    assert resp_kept.status_code == 200
    assert resp_kept.json() == {
        "repo": "acme/widget",
        "active": False,
        "base_sha": "sha-kept-2",
        "latest_verdict": {
            "id": "v2",
            "candidate_id": "c2",
            "keep": True,
            "speedup": 2.1,
            "p_value": 0.004,
            "reason": "2.10x faster, caught 3/3 bugs (SLA met)",
            "median_seconds": 45.0,
            "p95_seconds": 80.0,
            "sla_met": True,
        },
        "sla_met": True,
    }


def test_repo_status_rejected_candidate_does_not_skew_sla() -> None:
    ledger = InMemoryLedger()
    hyp1 = Hypothesis(
        id="h1",
        repo="acme/widget",
        description="Aggressive shortcut",
        skill="affected-tests",
        expected_savings_seconds=40.0,
        base_sha="sha-base-1",
    )
    cand1 = Candidate(
        id="c1",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/shortcut",
        base_sha="sha-base-1",
        head_sha="sha-bad",
        summary="Skipped tests",
    )
    verdict1 = Verdict(
        id="v1",
        candidate_id="c1",
        keep=False,
        speedup=2.0,
        p_value=0.001,
        median_seconds=45.0,
        p95_seconds=50.0,
        sla_met=True,
        reason="missed replayed bug",
    )
    for rec in (hyp1, cand1, verdict1):
        ledger.add(rec)

    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        resp = client.get("/api/repos/acme/widget/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["sla_met"] is False
    assert body["latest_verdict"]["id"] == "v1"
    assert body["latest_verdict"]["sla_met"] is True

    # Now add a kept candidate meeting SLA, followed by a rejected candidate not meeting SLA.
    cand2 = Candidate(
        id="c2",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/good",
        base_sha="sha-base-1",
        head_sha="sha-good",
        summary="Good optimization",
    )
    verdict2 = Verdict(
        id="v2",
        candidate_id="c2",
        keep=True,
        speedup=2.0,
        p_value=0.001,
        median_seconds=45.0,
        p95_seconds=55.0,
        sla_met=True,
        reason="fast and safe",
    )
    cand3 = Candidate(
        id="c3",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/slow",
        base_sha="sha-good",
        head_sha="sha-slow",
        summary="Slower experiment",
    )
    verdict3 = Verdict(
        id="v3",
        candidate_id="c3",
        keep=False,
        speedup=0.9,
        p_value=0.5,
        median_seconds=90.0,
        p95_seconds=130.0,
        sla_met=False,
        reason="slower",
    )
    for rec in (cand2, verdict2, cand3, verdict3):
        ledger.add(rec)

    with TestClient(app) as client:
        resp2 = client.get("/api/repos/acme/widget/status")

    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["base_sha"] == "sha-good"
    assert body2["sla_met"] is True
    assert body2["latest_verdict"]["id"] == "v3"
    assert body2["latest_verdict"]["keep"] is False


def test_repo_status_preserves_base_sha_when_kept_candidate_lacks_shas() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(
        id="h1",
        repo="acme/widget",
        description="Cache deps",
        skill="dep-cache",
        expected_savings_seconds=20.0,
        base_sha="abc123",
    )
    cand = Candidate(
        id="c1",
        hypothesis_id="h1",
        repo="acme/widget",
        branch="pit-crew/dep-cache",
        summary="Added cache without recording SHAs",
    )
    verdict = Verdict(
        id="v1",
        candidate_id="c1",
        keep=True,
        speedup=1.5,
        p_value=0.01,
        sla_met=True,
        reason="faster",
    )
    for rec in (hyp, cand, verdict):
        ledger.add(rec)

    app = create_app(ledger=ledger)
    with TestClient(app) as client:
        resp = client.get("/api/repos/acme/widget/status")

    assert resp.status_code == 200
    assert resp.json()["base_sha"] == "abc123"


def test_repo_casing_normalized_for_locks_and_telemetry() -> None:
    ledger = InMemoryLedger()
    ledger.add(
        Hypothesis(
            id="h1",
            repo="acme/widget",
            description="Cache deps",
            skill="dep-cache",
            expected_savings_seconds=20.0,
            base_sha="abc123",
        )
    )
    dispatcher = _StubDispatcher()
    app = create_app(ledger=ledger, dispatcher=dispatcher)

    with TestClient(app) as client:
        first = client.post("/triggers/cycle", json={"repo": "Acme/Widget"})
        assert first.status_code == 202
        assert dispatcher.started.wait(timeout=5)

        conflict = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert conflict.status_code == 409

        status_resp = client.get("/api/repos/ACME/WIDGET/status")
        assert status_resp.status_code == 200
        assert status_resp.json()["active"] is True
        assert status_resp.json()["base_sha"] == "abc123"

        attempts_resp = client.get("/api/repos/Acme/Widget/attempts")
        assert attempts_resp.status_code == 200
        assert len(attempts_resp.json()) == 1

        records_resp = client.get("/api/repos/Acme/Widget/records")
        assert records_resp.status_code == 200
        assert len(records_resp.json()["hypotheses"]) == 1

        dispatcher.release.set()
        assert dispatcher.finished.wait(timeout=5)
        while "acme/widget" in app.state.active_repos:
            client.get("/health")


def test_api_token_enforces_bearer_auth_on_api_and_triggers() -> None:
    dispatcher = _AsyncStubDispatcher()
    app = create_app(ledger=InMemoryLedger(), dispatcher=dispatcher, api_token="secret-api-token")  # noqa: S106
    auth_headers = {"Authorization": "Bearer secret-api-token"}

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        for method, path in (
            ("GET", "/api/repos/acme/widget/attempts"),
            ("GET", "/api/repos/acme/widget/records"),
            ("GET", "/api/repos/acme/widget/status"),
        ):
            missing = client.request(method, path)
            assert missing.status_code == 401
            assert missing.headers.get("www-authenticate") == "Bearer"

            wrong = client.request(method, path, headers={"Authorization": "Bearer wrong"})
            assert wrong.status_code == 401
            assert wrong.headers.get("www-authenticate") == "Bearer"

            basic = client.request(method, path, headers={"Authorization": "Basic secret-api-token"})
            assert basic.status_code == 401
            assert basic.headers.get("www-authenticate") == "Bearer"

            assert client.request(method, path, headers=auth_headers).status_code == 200

        missing_trigger = client.post("/triggers/cycle", json={"repo": "acme/widget"})
        assert missing_trigger.status_code == 401
        assert missing_trigger.headers.get("www-authenticate") == "Bearer"

        wrong_trigger = client.post(
            "/triggers/cycle",
            json={"repo": "acme/widget"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert wrong_trigger.status_code == 401
        assert wrong_trigger.headers.get("www-authenticate") == "Bearer"

        ok = client.post("/triggers/cycle", json={"repo": "acme/widget"}, headers=auth_headers)
        assert ok.status_code == 202
        assert dispatcher.done.wait(timeout=5)


def test_openapi_declares_http_bearer_security_scheme() -> None:
    app = create_app(ledger=InMemoryLedger(), api_token="secret-api-token")  # noqa: S106
    schema = app.openapi()

    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert security_schemes.get("HTTPBearer") == {"type": "http", "scheme": "bearer"}

    for path, method in (
        ("/triggers/cycle", "post"),
        ("/api/repos/{owner}/{name}/attempts", "get"),
        ("/api/repos/{owner}/{name}/records", "get"),
        ("/api/repos/{owner}/{name}/status", "get"),
    ):
        assert {"HTTPBearer": []} in schema["paths"][path][method].get("security", [])

    assert "security" not in schema["paths"]["/health"]["get"]
