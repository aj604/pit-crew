import hashlib
import hmac

from fastapi.testclient import TestClient

from pit_crew.ledger import InMemoryLedger
from pit_crew.server import create_app

_SECRET = "test-webhook-secret"  # noqa: S105


def _sign(payload: bytes, secret: str = _SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_webhook_valid_workflow_run_signature() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'{"action": "completed"}'
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


def test_webhook_missing_signature_returns_401() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'{"action": "completed"}'
    headers = {
        "X-GitHub-Delivery": "delivery-missing-sig",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 401


def test_webhook_invalid_signature_returns_401() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'{"action": "completed"}'
    headers = {
        "X-Hub-Signature-256": _sign(payload, secret="wrong-secret"),  # noqa: S106
        "X-GitHub-Delivery": "delivery-invalid-sig",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 401


def test_webhook_unconfigured_secret_returns_401() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=None)
    payload = b'{"action": "completed"}'
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-no-secret",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 401


def test_webhook_duplicate_delivery_returns_duplicate() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'{"action": "completed"}'
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-dup-1",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        first = client.post("/webhooks/github", content=payload, headers=headers)
        second = client.post("/webhooks/github", content=payload, headers=headers)

    assert first.status_code == 200
    assert first.json() == {"status": "accepted"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}


def test_webhook_ignored_non_workflow_run_event_returns_200() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'{"zen": "Keep it logically awesome."}'
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-ping-1",
        "X-GitHub-Event": "ping",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "event": "ping"}


def test_webhook_without_delivery_header_is_accepted() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'{"action": "completed"}'
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
