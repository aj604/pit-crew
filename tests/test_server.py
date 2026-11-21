import threading
from pathlib import Path

from fastapi.testclient import TestClient

from pit_crew.ledger import InMemoryLedger, SqliteLedger
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
