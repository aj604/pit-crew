"""FastAPI HTTP server scaffold for Pit Crew."""

import asyncio
import hmac
import inspect
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol, get_args

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.demo import DemoEngine, create_demo_router
from pit_crew.ledger import Ledger, SqliteLedger
from pit_crew.proof_bridge import MEDIA_URL_PREFIX, is_safe_mp4_filename
from pit_crew.run_tracker import RunTracker
from pit_crew.webhooks import create_github_webhook_router

logger = logging.getLogger(__name__)

DEFAULT_WEB_DIR = Path(__file__).resolve().parent / "web"
"""Pit Wall shell assets (``index.html``, ``styles.css``, ``app.js``) shipped inside the package."""


def _resolve_default_brand_dir() -> Path:
    """Resolve the default ``brand/`` directory across checkouts, wheels, and containers."""
    repo_dir = Path(__file__).resolve().parents[2] / "brand"
    if repo_dir.is_dir():
        return repo_dir
    pkg_dir = Path(__file__).resolve().parent / "brand"
    if pkg_dir.is_dir():
        return pkg_dir
    env_dir = os.environ.get("PIT_CREW_BRAND_DIR", "").strip()
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir)
    cwd_dir = Path.cwd() / "brand"
    if cwd_dir.is_dir():
        return cwd_dir
    return repo_dir


DEFAULT_BRAND_DIR = _resolve_default_brand_dir()
"""Repository-root or packaged brand assets; absent when running from a minimal wheel."""


def _normalize_repo(repo: str) -> str:
    """Normalize a repository identifier for case-insensitive lock and telemetry matching."""
    return repo.strip().lower()


class DispatcherProtocol(Protocol):
    """Protocol for dispatcher instances wired into the FastAPI server.

    To report live progress, a dispatcher adds an optional keyword argument
    ``on_progress: ProgressCallback | None = None`` to either method; the server
    detects it at call time and passes a :class:`ProgressCallback`.
    """

    def run_cycle(self, repo: str) -> object:
        """Run a single optimization cycle for a repository."""
        ...

    def run_until_sla(self, repo: str) -> object:
        """Run optimization cycles for a repository until its SLA is met."""
        ...


CycleStage = Literal["analysis", "optimizer", "proof", "timing", "replay", "rating"]
CYCLE_STAGES: tuple[CycleStage, ...] = get_args(CycleStage)


class ProgressCallback(Protocol):
    """Callback a dispatcher calls to report live cycle progress.

    Dispatchers opt in by accepting an ``on_progress`` keyword argument on
    ``run_cycle`` / ``run_until_sla``. Dispatchers that do not accept it are
    still called without it, so their status shows ``active`` with no stage.
    """

    def __call__(self, stage: CycleStage, completed_runs: int = 0, total_runs: int = 0) -> None:
        """Report the active stage and how many workflow runs have completed."""
        ...


@dataclass(frozen=True)
class CycleProgress:
    """Snapshot of a running cycle's stage and workflow-run counts."""

    stage: CycleStage | None = None
    completed_runs: int = 0
    total_runs: int = 0


IDLE_PROGRESS = CycleProgress()


class CycleProgressBoard:
    """Thread-safe per-repo progress state for running cycles.

    Each cycle gets a fresh token from :meth:`start`. Updates and clears carry
    that token, so a reporter left over from a finished or cancelled cycle can
    never overwrite the progress of a later cycle for the same repo.
    """

    def __init__(self) -> None:
        """Create an empty board."""
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[object, CycleProgress]] = {}

    def start(self, repo_key: str) -> object:
        """Begin tracking a new cycle for ``repo_key`` and return its token."""
        token = object()
        with self._lock:
            self._entries[repo_key] = (token, IDLE_PROGRESS)
        return token

    def update(self, repo_key: str, token: object, progress: CycleProgress) -> bool:
        """Record ``progress`` if ``token`` still owns ``repo_key``; return whether it did."""
        with self._lock:
            entry = self._entries.get(repo_key)
            if entry is None or entry[0] is not token:
                return False
            self._entries[repo_key] = (token, progress)
            return True

    def clear(self, repo_key: str, token: object) -> None:
        """Drop progress for ``repo_key`` if ``token`` still owns it."""
        with self._lock:
            entry = self._entries.get(repo_key)
            if entry is not None and entry[0] is token:
                del self._entries[repo_key]

    def get(self, repo_key: str) -> CycleProgress:
        """Return the current progress for ``repo_key``, or idle defaults."""
        with self._lock:
            entry = self._entries.get(repo_key)
        return entry[1] if entry is not None else IDLE_PROGRESS

    def reporter(self, repo_key: str, token: object) -> ProgressCallback:
        """Build the ``on_progress`` callback handed to the dispatcher for one cycle."""

        def on_progress(stage: CycleStage, completed_runs: int = 0, total_runs: int = 0) -> None:
            if not _valid_progress(stage, completed_runs, total_runs):
                logger.warning(
                    "Ignoring invalid progress for %s: stage=%r completed_runs=%r total_runs=%r",
                    repo_key,
                    stage,
                    completed_runs,
                    total_runs,
                )
                return
            progress = CycleProgress(stage=stage, completed_runs=completed_runs, total_runs=total_runs)
            if not self.update(repo_key, token, progress):
                logger.debug("Ignoring progress for %s from a finished cycle", repo_key)

        return on_progress


def _valid_progress(stage: object, completed_runs: object, total_runs: object) -> bool:
    """Return whether a progress report has a known stage and sane run counts."""
    if stage not in CYCLE_STAGES:
        return False
    if type(completed_runs) is not int or type(total_runs) is not int:
        return False
    return 0 <= completed_runs <= total_runs


def _accepts_on_progress(fn: Callable[..., object]) -> bool:
    """Return whether ``fn`` can be called with an ``on_progress`` keyword argument."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    for param in params:
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "on_progress" and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


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
    progress_board: CycleProgressBoard | None = None,
) -> None:
    """Execute a dispatcher cycle, reporting progress, and always release the per-repo lock."""
    repo_key = _normalize_repo(repo)
    board = progress_board if progress_board is not None else CycleProgressBoard()
    token = board.start(repo_key)
    try:
        fn: Callable[..., object] = dispatcher.run_until_sla if until_sla else dispatcher.run_cycle
        kwargs: dict[str, Any] = {}
        if _accepts_on_progress(fn):
            kwargs["on_progress"] = board.reporter(repo_key, token)
        if inspect.iscoroutinefunction(fn):
            await fn(repo, **kwargs)
        else:
            result = await asyncio.to_thread(fn, repo, **kwargs)
            if inspect.isawaitable(result):
                await result
    except Exception:
        logger.exception("Cycle failed for %s", repo)
    finally:
        # Clear progress before releasing the lock so an idle status never shows a stale stage.
        board.clear(repo_key, token)
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


def _resolve_preview_url(repo: str, head_sha: str, raw_path: object) -> str:
    """Resolve a relative committed proof preview path to a raw GitHub URL, or keep safe URLs."""
    if not isinstance(raw_path, str):
        return ""
    path = raw_path.strip()
    if not path:
        return ""
    if path.startswith(("https://", "http://", "/web/", "/brand/")):
        return path
    if head_sha and "/" in repo and not path.startswith("/") and ".." not in path.split("/"):
        return f"https://raw.githubusercontent.com/{repo}/{head_sha}/{path}"
    return ""


def _resolve_video_url(raw_url: object) -> str:
    """Resolve a playable replay MP4 URL (deployed ``/media/*.mp4`` or ``http(s)://``)."""
    if not isinstance(raw_url, str):
        return ""
    value = raw_url.strip()
    if not value:
        return ""
    if value.startswith(("https://", "http://")):
        return value
    prefix = f"{MEDIA_URL_PREFIX}/"
    if value.startswith(prefix) and is_safe_mp4_filename(value[len(prefix) :]):
        return value
    return ""


def _resolve_media_dir(ledger: Ledger, media_dir: Path | None) -> Path | None:
    """Resolve the persistent MP4 media directory for the server."""
    if media_dir is not None:
        return media_dir
    env_dir = os.environ.get("PIT_CREW_MEDIA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    if isinstance(ledger, SqliteLedger):
        return ledger.path.resolve().parent / "media"
    return None


def _resolve_media_file(media_dir: Path | None, filename: str) -> Path:
    """Validate ``filename`` and resolve an existing ``.mp4`` inside ``media_dir``."""
    if media_dir is None or not is_safe_mp4_filename(filename):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Replay video not found")
    root = media_dir.resolve()
    candidate = media_dir / filename
    if candidate.is_symlink():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Replay video not found")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Replay video not found")
    return resolved


def _make_media_token_dependency(
    api_token: str,
) -> Callable[[HTTPAuthorizationCredentials | None, str | None], None]:
    """Build a FastAPI dependency enforcing Bearer header or ``?token=`` query auth on ``/media/*``."""

    def _require_media_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
        token: Annotated[str | None, Query()] = None,
    ) -> None:
        provided = ""
        if credentials is not None and credentials.scheme.lower() == "bearer" and credentials.credentials:
            provided = credentials.credentials
        elif token:
            provided = token.strip()
        if not provided or not hmac.compare_digest(provided, api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return _require_media_token


def _enrich_bug_outcome(repo: str, head_sha: str, outcome: dict[str, Any]) -> dict[str, Any]:
    """Ensure attempt detail bug_outcomes expose multimodal fields, preview URLs, and MP4 video URLs."""
    enriched = dict(outcome)
    enriched.setdefault("modality", "")
    enriched.setdefault("visual_evidence_seen", False)
    enriched.setdefault("visual_summary", str(enriched.get("seen") or ""))
    enriched.setdefault("baseline_artifact_urls", [])
    enriched.setdefault("candidate_artifact_urls", [])
    enriched.setdefault("baseline_preview_path", "")
    enriched.setdefault("candidate_preview_path", "")
    enriched["baseline_preview_url"] = _resolve_preview_url(
        repo,
        head_sha,
        enriched.get("baseline_preview_url") or enriched.get("baseline_preview_path"),
    )
    enriched["candidate_preview_url"] = _resolve_preview_url(
        repo,
        head_sha,
        enriched.get("candidate_preview_url") or enriched.get("candidate_preview_path"),
    )
    enriched["baseline_video_url"] = _resolve_video_url(enriched.get("baseline_video_url"))
    enriched["candidate_video_url"] = _resolve_video_url(enriched.get("candidate_video_url"))
    return enriched


def _attempt_detail(ledger: Ledger, repo: str, candidate_id: str) -> dict[str, Any] | None:
    """Build detailed telemetry and multimodal visual proof payload for one attempt."""
    repo_key = _normalize_repo(repo)
    hypotheses = {h.id: h for h in ledger.hypotheses_for_repo(repo)}
    candidate = next(
        (
            c
            for c in ledger.all(Candidate)
            if c.id == candidate_id and (c.hypothesis_id in hypotheses or _normalize_repo(c.repo) == repo_key)
        ),
        None,
    )
    if candidate is None:
        return None

    hypothesis = hypotheses.get(candidate.hypothesis_id)
    replay_plan = next((p for p in reversed(ledger.all(ReplayPlan)) if p.candidate_id == candidate_id), None)
    evidence = next((e for e in reversed(ledger.all(Evidence)) if e.candidate_id == candidate_id), None)
    verdict = next((v for v in reversed(ledger.all(Verdict)) if v.candidate_id == candidate_id), None)
    attempt = next((a for a in ledger.prior_attempts(repo) if a.get("candidate_id") == candidate_id), None)

    evidence_payload: dict[str, Any] | None = None
    visual_proof: list[dict[str, Any]] = []
    if evidence is not None:
        evidence_payload = asdict(evidence)
        enriched_outcomes = [_enrich_bug_outcome(repo, candidate.head_sha, row) for row in evidence.bug_outcomes]
        evidence_payload["bug_outcomes"] = enriched_outcomes
        visual_proof = [
            {
                "ref": str(row.get("ref") or row.get("sha") or row.get("commit_sha") or ""),
                "check": str(row.get("check") or ""),
                "status": str(row.get("status") or ("caught" if row.get("caught") else "missed")),
                "modality": row["modality"],
                "visual_evidence_seen": bool(row["visual_evidence_seen"]),
                "visual_summary": str(row["visual_summary"]),
                "baseline_artifact_urls": list(row["baseline_artifact_urls"]),
                "candidate_artifact_urls": list(row["candidate_artifact_urls"]),
                "baseline_preview_path": str(row["baseline_preview_path"]),
                "candidate_preview_path": str(row["candidate_preview_path"]),
                "baseline_preview_url": row["baseline_preview_url"],
                "candidate_preview_url": row["candidate_preview_url"],
                "baseline_video_url": row["baseline_video_url"],
                "candidate_video_url": row["candidate_video_url"],
            }
            for row in enriched_outcomes
        ]

    return {
        "repo": repo,
        "candidate_id": candidate_id,
        "attempt": attempt,
        "hypothesis": asdict(hypothesis) if hypothesis is not None else None,
        "candidate": asdict(candidate),
        "replay_plan": asdict(replay_plan) if replay_plan is not None else None,
        "evidence": evidence_payload,
        "verdict": asdict(verdict) if verdict is not None else None,
        "visual_proof": visual_proof,
    }


def _repo_status(
    ledger: Ledger,
    repo: str,
    active_repos: set[str],
    progress_board: CycleProgressBoard | None = None,
) -> dict[str, Any]:
    """Build the Pit Wall status payload for ``repo``."""
    repo_key = _normalize_repo(repo)
    active = repo_key in active_repos
    progress = progress_board.get(repo_key) if active and progress_board is not None else IDLE_PROGRESS
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
        "active": active,
        "base_sha": base_sha,
        "latest_verdict": asdict(latest_verdict) if latest_verdict is not None else None,
        "sla_met": latest_kept_verdict.sla_met if latest_kept_verdict is not None else False,
        "stage": progress.stage,
        "completed_runs": progress.completed_runs,
        "total_runs": progress.total_runs,
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


def create_app(  # noqa: PLR0913  keyword-only asset dirs keep existing positional callers working
    ledger: Ledger,
    dispatcher: DispatcherProtocol | None = None,
    webhook_secret: str | None = None,
    run_tracker: RunTracker | None = None,
    api_token: str | None = None,
    *,
    brand_dir: Path | None = None,
    web_dir: Path | None = None,
    media_dir: Path | None = None,
) -> FastAPI:
    """Create and configure the Pit Crew FastAPI application.

    The Pit Wall shell is served at ``/`` with its assets under ``/web``. Brand assets are mounted at
    ``/brand`` only when ``brand_dir`` exists, so an installed wheel without the repository's
    ``brand/`` directory still starts cleanly. Persisted MP4 replay recordings are served at
    ``/media/{filename}`` with HTTP Range streaming and optional ``?download=1`` attachment download.
    """
    app = FastAPI(title="pit-crew")
    active_repos: set[str] = set()
    background_tasks: set[asyncio.Task[None]] = set()
    progress_board = CycleProgressBoard()
    tracker = run_tracker if run_tracker is not None else RunTracker()
    resolved_media_dir = _resolve_media_dir(ledger, media_dir)
    engine = DemoEngine()
    app.state.ledger = ledger
    app.state.dispatcher = dispatcher
    app.state.webhook_secret = webhook_secret
    app.state.run_tracker = tracker
    app.state.api_token = api_token
    app.state.media_dir = resolved_media_dir
    app.state.active_repos = active_repos
    app.state.background_tasks = background_tasks
    app.state.progress_board = progress_board
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
                until_sla=payload.until_sla,
                active_repos=active_repos,
                progress_board=progress_board,
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
        "/api/repos/{owner}/{name}/attempts/{candidate_id}",
        dependencies=api_auth_dependencies,
    )
    def repo_attempt_detail(owner: str, name: str, candidate_id: str) -> dict[str, Any]:
        """Return detailed telemetry, artifact modality, visual summaries, and preview URLs for one attempt."""
        detail = _attempt_detail(ledger, f"{owner}/{name}", candidate_id)
        if detail is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Candidate {candidate_id!r} not found for {owner}/{name}",
            )
        return detail

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
        """Return cycle state and live progress, base SHA, latest verdict, and SLA status."""
        return _repo_status(ledger, f"{owner}/{name}", active_repos, progress_board)

    _register_media_route(app, media_dir=resolved_media_dir, api_token=api_token)
    resolved_brand = brand_dir if brand_dir is not None else _resolve_default_brand_dir()
    _mount_pit_wall(app, brand_dir=resolved_brand, web_dir=web_dir or DEFAULT_WEB_DIR)
    return app


def _register_media_route(app: FastAPI, *, media_dir: Path | None, api_token: str | None) -> None:
    """Register ``GET /media/{filename}`` for Range-streamed and downloadable MP4 replays."""
    cache_control = "private, max-age=3600" if api_token is not None else "public, max-age=3600"
    media_auth_deps = [Depends(_make_media_token_dependency(api_token))] if api_token is not None else []

    @app.get(f"{MEDIA_URL_PREFIX}/{{filename}}", dependencies=media_auth_deps)
    def replay_media(
        filename: str,
        *,
        download: Annotated[bool, Query()] = False,
    ) -> FileResponse:
        """Stream or download a persisted MP4 bug-replay video (supports HTTP Range seeking)."""
        target = _resolve_media_file(media_dir, filename)
        return FileResponse(
            target,
            media_type="video/mp4",
            filename=filename,
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": cache_control},
        )


def _mount_pit_wall(app: FastAPI, *, brand_dir: Path, web_dir: Path) -> None:
    """Serve the Pit Wall shell at ``/`` and mount its static asset directories."""
    index_html = web_dir / "index.html"

    @app.get("/", include_in_schema=False)
    def pit_wall() -> FileResponse:
        """Return the single-page Pit Wall shell."""
        return FileResponse(index_html, media_type="text/html")

    app.mount("/web", StaticFiles(directory=web_dir), name="web")
    if brand_dir.is_dir():
        app.mount("/brand", StaticFiles(directory=brand_dir), name="brand")
    else:
        logger.warning("Brand directory %s not found; /brand assets will not be served", brand_dir)
