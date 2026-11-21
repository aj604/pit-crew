from pathlib import Path

from fastapi.testclient import TestClient

from pit_crew.ledger import InMemoryLedger, SqliteLedger
from pit_crew.server import create_app


class _StubDispatcher:
    pass


def test_health_in_memory_ledger() -> None:
    app = create_app(ledger=InMemoryLedger(), dispatcher=_StubDispatcher(), webhook_secret="test-secret")  # noqa: S106
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ledger": "memory"}


def test_health_sqlite_ledger(tmp_path: Path) -> None:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    app = create_app(ledger=ledger)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ledger": "sqlite"}
