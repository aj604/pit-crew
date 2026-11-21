"""FastAPI HTTP server scaffold for Pit Crew."""

import asyncio
import hmac
import inspect
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pit_crew.contracts import Candidate, Hypothesis, Verdict
from pit_crew.demo import DemoEngine, create_demo_router
from pit_crew.ledger import Ledger, SqliteLedger
from pit_crew.run_tracker import RunTracker
from pit_crew.webhooks import create_github_webhook_router

logger = logging.getLogger(__name__)


def _normalize_repo(repo: str) -> str:
    """Normalize a repository identifier for case-insensitive lock and telemetry matching."""
    return repo.strip().lower()


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
    repo_key: str,
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
        active_repos.discard(repo_key)


def _current_base_sha(
    latest_kept: Candidate | None,
    candidates: list[Candidate],
    hypotheses: list[Hypothesis],
) -> str | None:
    """Resolve the current base SHA for a repository from its ledger records."""
    if latest_kept is not None:
        if latest_kept.head_sha:
            return latest_kept.head_sha
        if latest_kept.base_sha:
            return latest_kept.base_sha
        hypotheses_by_id = {h.id: h for h in hypotheses}
        linked_hyp = hypotheses_by_id.get(latest_kept.hypothesis_id)
        if linked_hyp is not None and linked_hyp.base_sha:
            return linked_hyp.base_sha
    for cand in reversed(candidates):
        if cand.base_sha:
            return cand.base_sha
    for hyp in reversed(hypotheses):
        if hyp.base_sha:
            return hyp.base_sha
    return None


def _repo_status(ledger: Ledger, repo: str, active_repos: set[str]) -> dict[str, Any]:
    """Build the Pit Wall status payload for ``repo``."""
    repo_key = _normalize_repo(repo)
    hypotheses = ledger.hypotheses_for_repo(repo)
    hypothesis_ids = {h.id for h in hypotheses}
    repo_candidates = [
        c for c in ledger.all(Candidate) if c.hypothesis_id in hypothesis_ids or _normalize_repo(c.repo) == repo_key
    ]
    candidate_ids = {c.id for c in repo_candidates}

    latest_kept = ledger.latest_kept_candidate(repo)
    latest_verdict: Verdict | None = None
    latest_kept_verdict: Verdict | None = None
    for verdict in ledger.all(Verdict):
        if verdict.candidate_id in candidate_ids:
            latest_verdict = verdict
        if latest_kept is not None and verdict.candidate_id == latest_kept.id:
            latest_kept_verdict = verdict

    base_sha = _current_base_sha(latest_kept, repo_candidates, hypotheses)
    return {
        "repo": repo,
        "active": repo_key in active_repos,
        "base_sha": base_sha,
        "latest_verdict": asdict(latest_verdict) if latest_verdict is not None else None,
        "sla_met": latest_kept_verdict.sla_met if latest_kept_verdict is not None else False,
    }


_bearer_scheme = HTTPBearer(auto_error=False)


def _make_api_token_dependency(api_token: str) -> Callable[[HTTPAuthorizationCredentials | None], None]:
    """Build a FastAPI dependency that enforces Bearer token auth for ``api_token``."""

    def _require_api_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not credentials.credentials
            or not hmac.compare_digest(credentials.credentials, api_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _require_api_token


def create_app(
    ledger: Ledger,
    dispatcher: DispatcherProtocol | None = None,
    webhook_secret: str | None = None,
    run_tracker: RunTracker | None = None,
    api_token: str | None = None,
) -> FastAPI:
    """Create and configure the Pit Crew FastAPI application."""
    app = FastAPI(title="pit-crew")
    active_repos: set[str] = set()
    background_tasks: set[asyncio.Task[None]] = set()
    tracker = run_tracker if run_tracker is not None else RunTracker()
    engine = DemoEngine(ledger=ledger)
    app.state.ledger = ledger
    app.state.dispatcher = dispatcher
    app.state.webhook_secret = webhook_secret
    app.state.run_tracker = tracker
    app.state.api_token = api_token
    app.state.active_repos = active_repos
    app.state.background_tasks = background_tasks
    app.state.demo_engine = engine

    api_auth_dependencies = [Depends(_make_api_token_dependency(api_token))] if api_token is not None else []
    app.include_router(create_demo_router(engine, dependencies=api_auth_dependencies))

    @app.get("/health")
    def health() -> dict[str, str]:
        """Return service health and active ledger backend."""
        ledger_kind = "sqlite" if isinstance(ledger, SqliteLedger) else "memory"
        return {"status": "ok", "ledger": ledger_kind}

    app.include_router(create_github_webhook_router(webhook_secret=webhook_secret, run_tracker=tracker))

    @app.post(
        "/triggers/cycle",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=api_auth_dependencies,
    )
    async def trigger_cycle(payload: CycleTriggerRequest) -> dict[str, str]:
        """Enqueue a background optimization cycle guarded by a per-repo lock."""
        if dispatcher is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Dispatcher is not configured",
            )
        repo_key = _normalize_repo(payload.repo)
        if repo_key in active_repos:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cycle already active for {payload.repo}",
            )

        active_repos.add(repo_key)
        task = asyncio.create_task(
            _run_cycle_task(
                dispatcher,
                payload.repo,
                repo_key=repo_key,
                until_sla=payload.until_sla,
                active_repos=active_repos,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return {"status": "started", "repo": payload.repo}

    @app.get(
        "/api/repos/{owner}/{name}/attempts",
        dependencies=api_auth_dependencies,
    )
    def repo_attempts(owner: str, name: str) -> list[dict[str, Any]]:
        """Return prior optimization attempts for a repository."""
        return ledger.prior_attempts(f"{owner}/{name}")

    @app.get(
        "/api/repos/{owner}/{name}/records",
        dependencies=api_auth_dependencies,
    )
    def repo_records(owner: str, name: str) -> dict[str, list[dict[str, Any]]]:
        """Return structured ledger records for a repository."""
        return ledger.records_for_repo(f"{owner}/{name}")

    @app.get(
        "/api/repos/{owner}/{name}/status",
        dependencies=api_auth_dependencies,
    )
    def repo_status(owner: str, name: str) -> dict[str, Any]:
        """Return current cycle state, base SHA, latest verdict, and SLA status."""
        return _repo_status(ledger, f"{owner}/{name}", active_repos)

    return app
