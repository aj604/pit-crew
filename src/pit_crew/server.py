"""FastAPI HTTP server scaffold for Pit Crew."""

from typing import Protocol

from fastapi import FastAPI

from pit_crew.ledger import Ledger, SqliteLedger


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

    return app
