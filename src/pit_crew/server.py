"""FastAPI HTTP server scaffold for Pit Crew."""

from typing import Any, Protocol

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

    @app.get("/api/repos/{owner}/{name}/attempts")
    def repo_attempts(owner: str, name: str) -> list[dict[str, Any]]:
        """Return prior optimization attempts for a repository."""
        return ledger.prior_attempts(f"{owner}/{name}")

    @app.get("/api/repos/{owner}/{name}/records")
    def repo_records(owner: str, name: str) -> dict[str, list[dict[str, Any]]]:
        """Return structured ledger records for a repository."""
        return ledger.records_for_repo(f"{owner}/{name}")

    return app
