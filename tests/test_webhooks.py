import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from pit_crew.ledger import InMemoryLedger
from pit_crew.run_tracker import RunTracker, WorkflowRunResult
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

    assert response.status_code == 202
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

    assert first.status_code == 202
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

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}


def test_webhook_workflow_run_completed_wakes_run_tracker_waiter() -> None:
    tracker = RunTracker()
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET, run_tracker=tracker)
    assert app.state.run_tracker is tracker

    payload_dict = {
        "action": "completed",
        "workflow_run": {
            "id": 987654,
            "status": "completed",
            "conclusion": "success",
            "head_sha": "abc123def456",
            "html_url": "https://github.com/aj604/pit-crew/actions/runs/987654",
            "run_started_at": "2026-11-21T12:00:00Z",
            "updated_at": "2026-11-21T12:02:30Z",
        },
    }
    payload = json.dumps(payload_dict).encode("utf-8")
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-completed-1",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }

    with TestClient(app) as client:
        assert client.portal is not None
        wait_future = client.portal.start_task_soon(tracker.wait, 987654, 5.0)
        response = client.post("/webhooks/github", content=payload, headers=headers)
        result = wait_future.result(timeout=5.0)

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert result == WorkflowRunResult(
        run_id=987654,
        status="completed",
        conclusion="success",
        head_sha="abc123def456",
        html_url="https://github.com/aj604/pit-crew/actions/runs/987654",
        duration_seconds=150.0,
        raw_payload=payload_dict,
    )


def test_webhook_workflow_run_non_completed_actions_ignored_and_return_200() -> None:
    tracker = RunTracker()
    tracker.register(4242)
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET, run_tracker=tracker)

    with TestClient(app) as client:
        for idx, action in enumerate(("requested", "in_progress")):
            payload = json.dumps(
                {
                    "action": action,
                    "workflow_run": {"id": 4242, "status": action},
                }
            ).encode("utf-8")
            headers = {
                "X-Hub-Signature-256": _sign(payload),
                "X-GitHub-Delivery": f"delivery-ignored-{idx}",
                "X-GitHub-Event": "workflow_run",
                "Content-Type": "application/json",
            }
            response = client.post("/webhooks/github", content=payload, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"status": "ignored", "event": "workflow_run", "action": action}

    assert tracker.get(4242) is None


def test_webhook_default_run_tracker_exposed_on_app_state() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    assert isinstance(app.state.run_tracker, RunTracker)


def test_webhook_workflow_run_invalid_json_returns_400() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b"{not-json"
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-bad-json",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 400


def test_webhook_workflow_run_non_object_json_returns_400() -> None:
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET)
    payload = b'["not", "an", "object"]'
    headers = {
        "X-Hub-Signature-256": _sign(payload),
        "X-GitHub-Delivery": "delivery-list-json",
        "X-GitHub-Event": "workflow_run",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        response = client.post("/webhooks/github", content=payload, headers=headers)

    assert response.status_code == 400


def test_webhook_workflow_run_completed_with_invalid_timestamps_and_bool_id() -> None:
    tracker = RunTracker()
    tracker.register(777)
    app = create_app(ledger=InMemoryLedger(), webhook_secret=_SECRET, run_tracker=tracker)

    bad_ts_payload = json.dumps(
        {
            "action": "completed",
            "workflow_run": {
                "id": 777,
                "status": "completed",
                "run_started_at": "not-a-timestamp",
                "updated_at": "also-not-a-timestamp",
            },
        }
    ).encode("utf-8")
    bool_id_payload = json.dumps(
        {
            "action": "completed",
            "workflow_run": {"id": True, "status": "completed"},
        }
    ).encode("utf-8")

    with TestClient(app) as client:
        res1 = client.post(
            "/webhooks/github",
            content=bad_ts_payload,
            headers={
                "X-Hub-Signature-256": _sign(bad_ts_payload),
                "X-GitHub-Delivery": "delivery-bad-ts",
                "X-GitHub-Event": "workflow_run",
            },
        )
        res2 = client.post(
            "/webhooks/github",
            content=bool_id_payload,
            headers={
                "X-Hub-Signature-256": _sign(bool_id_payload),
                "X-GitHub-Delivery": "delivery-bool-id",
                "X-GitHub-Event": "workflow_run",
            },
        )

    assert res1.status_code == 202
    resolved = tracker.get(777)
    assert resolved is not None
    assert resolved.duration_seconds is None
    assert res2.status_code == 202
