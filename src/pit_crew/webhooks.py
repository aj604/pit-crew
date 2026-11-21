"""GitHub webhook endpoint with HMAC-SHA256 verification and delivery deduplication."""

import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request, status

_SIGNATURE_PREFIX = "sha256="


def _verify_github_signature(payload: bytes, signature: str | None, secret: str | None) -> bool:
    """Verify the GitHub ``X-Hub-Signature-256`` header using constant-time comparison."""
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    expected = f"{_SIGNATURE_PREFIX}{digest}"
    return hmac.compare_digest(signature, expected)


def create_github_webhook_router(webhook_secret: str | None = None) -> APIRouter:
    """Create a FastAPI router exposing ``POST /webhooks/github``."""
    router = APIRouter()
    seen_deliveries: set[str] = set()

    @router.post("/webhooks/github")
    async def github_webhook(request: Request) -> dict[str, str]:
        """Verify GitHub webhook signatures, deduplicate deliveries, and acknowledge events."""
        raw_body = await request.body()
        signature = request.headers.get("X-Hub-Signature-256")
        if not _verify_github_signature(raw_body, signature, webhook_secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-Hub-Signature-256 header",
            )

        delivery_id = request.headers.get("X-GitHub-Delivery")
        if delivery_id:
            if delivery_id in seen_deliveries:
                return {"status": "duplicate"}
            seen_deliveries.add(delivery_id)

        event_type = request.headers.get("X-GitHub-Event", "")
        if event_type != "workflow_run":
            return {"status": "ignored", "event": event_type}

        return {"status": "accepted"}

    return router
