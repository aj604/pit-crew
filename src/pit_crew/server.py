"""FastAPI HTTP server scaffold for Pit Crew."""

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, status

from pit_crew.ledger import Ledger, SqliteLedger

logger = logging.getLogger(__name__)


class DispatcherProtocol(Protocol):
    """Protocol for dispatcher instances wired into the FastAPI server."""

    def run_cycle(self, repo: str) -> object:
        """Run a single optimization cycle for a repository."""
        ...

    def run_until_sla(self, repo: str) -> object:
        """Run optimization cycles for a repository until its SLA is met."""
        ...


@dataclass(frozen=True)
class CycleTriggerRequest:
    """Request payload for triggering an optimization cycle."""

    repo: str
    until_sla: bool = False


async def _run_cycle_task(
    dispatcher: DispatcherProtocol,
    repo: str,
    *,
    until_sla: bool,
    active_repos: set[str],
) -> None:
    """Execute a dispatcher cycle and always release the per-repo lock."""
    try:
        fn = dispatcher.run_until_sla if until_sla else dispatcher.run_cycle
        if inspect.iscoroutinefunction(fn):
            await fn(repo)
        else:
            result = await asyncio.to_thread(fn, repo)
            if inspect.isawaitable(result):
                await result
    except Exception:
        logger.exception("Cycle failed for %s", repo)
    finally:
        active_repos.discard(repo)


def create_app(
    ledger: Ledger,
    dispatcher: DispatcherProtocol | None = None,
    webhook_secret: str | None = None,
) -> FastAPI:
    """Create and configure the Pit Crew FastAPI application."""
    app = FastAPI(title="pit-crew")
    active_repos: set[str] = set()
    background_tasks: set[asyncio.Task[None]] = set()
    app.state.ledger = ledger
    app.state.dispatcher = dispatcher
    app.state.webhook_secret = webhook_secret
    app.state.active_repos = active_repos
    app.state.background_tasks = background_tasks

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return service health and active ledger backend."""
        ledger_kind = "sqlite" if isinstance(ledger, SqliteLedger) else "memory"
        return {"status": "ok", "ledger": ledger_kind}

    @app.post("/triggers/cycle", status_code=status.HTTP_202_ACCEPTED)
    async def trigger_cycle(payload: CycleTriggerRequest) -> dict[str, str]:
        """Enqueue a background optimization cycle guarded by a per-repo lock."""
        if dispatcher is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dispatcher is not configured",
            )
        if payload.repo in active_repos:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cycle already active for {payload.repo}",
            )

        active_repos.add(payload.repo)
        task = asyncio.create_task(
            _run_cycle_task(
                dispatcher,
                payload.repo,
                until_sla=payload.until_sla,
                active_repos=active_repos,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {"status": "started", "repo": payload.repo}

    @app.get("/api/repos/{owner}/{name}/attempts")
    def repo_attempts(owner: str, name: str) -> list[dict[str, Any]]:
        """Return prior optimization attempts for a repository."""
        return ledger.prior_attempts(f"{owner}/{name}")

    @app.get("/api/repos/{owner}/{name}/records")
    def repo_records(owner: str, name: str) -> dict[str, list[dict[str, Any]]]:
        """Return structured ledger records for a repository."""
        return ledger.records_for_repo(f"{owner}/{name}")

    return app
