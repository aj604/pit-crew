"""GitHub webhook endpoint with HMAC-SHA256 verification and delivery deduplication."""

import hashlib
import hmac
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from pit_crew.run_tracker import RunTracker, WorkflowRunResult

_SIGNATURE_PREFIX = "sha256="
_DEFAULT_MAX_DELIVERIES = 10_000
_DEFAULT_DELIVERY_TTL_SECONDS = 3600.0
_DEFAULT_MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


class DeliveryCache:
    """Bounded TTL + LRU cache for GitHub webhook delivery IDs."""

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_DELIVERIES,
        ttl_seconds: float = _DEFAULT_DELIVERY_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the bounded delivery deduplication cache."""
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, float] = OrderedDict()

    def _evict_expired(self, now: float) -> None:
        while self._entries:
            _, recorded_at = next(iter(self._entries.items()))
            if now - recorded_at >= self._ttl_seconds:
                self._entries.popitem(last=False)
            else:
                break

    def check_and_record(self, delivery_id: str) -> bool:
        """Return True if ``delivery_id`` is a live duplicate; otherwise record it and return False."""
        now = self._clock()
        self._evict_expired(now)
        if delivery_id in self._entries:
            self._entries.move_to_end(delivery_id)
            self._entries[delivery_id] = now
            return True
        if self._max_entries > 0:
            self._entries[delivery_id] = now
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return False


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    """Stream the request body up to ``max_bytes``, raising HTTP 413 if exceeded."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Webhook payload exceeds maximum allowed size",
                )
        except ValueError:
            pass

    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook payload exceeds maximum allowed size",
            )
        buf.extend(chunk)
    return bytes(buf)


def _verify_github_signature(payload: bytes, signature: str | None, secret: str | None) -> bool:
    """Verify the GitHub ``X-Hub-Signature-256`` header using constant-time comparison."""
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    expected = f"{_SIGNATURE_PREFIX}{digest}"
    return hmac.compare_digest(signature, expected)


def _compute_duration_seconds(workflow_run: dict[str, Any]) -> float | None:
    """Compute workflow run duration in seconds from ISO-8601 timestamps."""
    started_raw = workflow_run.get("run_started_at") or workflow_run.get("created_at")
    updated_raw = workflow_run.get("updated_at")
    if not isinstance(started_raw, str) or not isinstance(updated_raw, str):
        return None
    try:
        started = datetime.fromisoformat(started_raw)
        updated = datetime.fromisoformat(updated_raw)
    except ValueError:
        return None
    return max(0.0, (updated - started).total_seconds())


def _extract_workflow_run_result(payload: dict[str, Any]) -> WorkflowRunResult | None:
    """Extract a ``WorkflowRunResult`` from a ``workflow_run`` webhook payload."""
    workflow_run = payload.get("workflow_run")
    if not isinstance(workflow_run, dict):
        return None
    run_id = workflow_run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int):
        return None

    run_status = workflow_run.get("status")
    conclusion = workflow_run.get("conclusion")
    head_sha = workflow_run.get("head_sha")
    html_url = workflow_run.get("html_url")
    return WorkflowRunResult(
        run_id=run_id,
        status=run_status if isinstance(run_status, str) else "completed",
        conclusion=conclusion if isinstance(conclusion, str) else None,
        head_sha=head_sha if isinstance(head_sha, str) else None,
        html_url=html_url if isinstance(html_url, str) else None,
        duration_seconds=_compute_duration_seconds(workflow_run),
        raw_payload=payload,
    )


def _parse_json_object(raw_body: bytes) -> dict[str, Any]:
    """Decode raw request bytes into a JSON object or raise HTTP 400."""
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Webhook payload must be a JSON object",
        )
    return payload


def create_github_webhook_router(
    webhook_secret: str | None = None,
    run_tracker: RunTracker | None = None,
    *,
    delivery_cache: DeliveryCache | None = None,
    max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES,
) -> APIRouter:
    """Create a FastAPI router exposing ``POST /webhooks/github``."""
    router = APIRouter()
    seen_deliveries = delivery_cache if delivery_cache is not None else DeliveryCache()

    @router.post("/webhooks/github")
    async def github_webhook(request: Request, response: Response) -> dict[str, str]:
        """Verify GitHub webhook signatures, deduplicate deliveries, and resolve tracked runs."""
        signature = request.headers.get("X-Hub-Signature-256")
        if not webhook_secret or not signature:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-Hub-Signature-256 header",
            )

        raw_body = await _read_limited_body(request, max_body_bytes)
        if not _verify_github_signature(raw_body, signature, webhook_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-Hub-Signature-256 header",
            )

        delivery_id = request.headers.get("X-GitHub-Delivery")
        if delivery_id and seen_deliveries.check_and_record(delivery_id):
            return {"status": "duplicate"}

        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type != "workflow_run":
            return {"status": "ignored", "event": event_type}

        payload = _parse_json_object(raw_body)
        action = payload.get("action")
        if action != "completed":
            return {
                "status": "ignored",
                "event": event_type,
                "action": str(action) if action is not None else "",
            }

        if run_tracker is not None:
            result = _extract_workflow_run_result(payload)
            if result is not None:
                run_tracker.resolve(result)

        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "accepted"}

    return router
