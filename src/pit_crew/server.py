"""FastAPI HTTP server scaffold for Pit Crew."""

from typing import Protocol

from fastapi import FastAPI

from pit_crew.ledger import Ledger, SqliteLedger
from pit_crew.webhooks import create_github_webhook_router


class DispatcherProtocol(Protocol):
    """Protocol for dispatcher instances wired into the FastAPI server."""


def create_app(
    ledger: Ledger,
    dispatcher: DispatcherProtocol | None = None,
    webhook_secret: str | None = None,
) -> FastAPI:
    """Create and configure the Pit Crew FastAPI application."""
    app = FastAPI(title="pit-crew")
    app.state.ledger = ledger
    app.state.dispatcher = dispatcher
    app.state.webhook_secret = webhook_secret

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return service health and active ledger backend."""
        ledger_kind = "sqlite" if isinstance(ledger, SqliteLedger) else "memory"
        return {"status": "ok", "ledger": ledger_kind}

    app.include_router(create_github_webhook_router(webhook_secret=webhook_secret))

    return app
