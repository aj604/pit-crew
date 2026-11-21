"""Live runtime CLI handlers: ``run-cycle``, ``serve``, and ``register-webhook``.

Kept in its own module so ``cli.py`` can wire ``add_runtime_subcommands`` and
``run_runtime_command`` in four lines without colliding with sibling CLI edits.
"""

import argparse
import io
import json
import logging
import os
import sys
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
import uvicorn

from pit_crew import managed_agent
from pit_crew import workspace as host_workspace
from pit_crew.contracts import Evidence, Verdict
from pit_crew.dispatcher import Dispatcher, DispatcherConfig
from pit_crew.github import (
    API_VERSION,
    DEFAULT_API_URL,
    DEFAULT_FAILED_LOG_MAX_BYTES,
    USER_AGENT,
    GitHubClient,
    HttpxGitHubClient,
    RunArtifact,
    RunArtifactsClient,
)
from pit_crew.lanes.proof.miner import JobRun
from pit_crew.ledger import Ledger, SqliteLedger
from pit_crew.proof_bridge import ProofBridge
from pit_crew.run_tracker import RunTracker
from pit_crew.server import create_app

DEFAULT_LEDGER_PATH = ".pit_crew/ledger.db"
_JOB_RUN_PAGE_SIZE = 30
_JOBS_PER_RUN_LIMIT = 100


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }


def _repo_api_path(repo: str) -> str:
    owner, sep, name = repo.strip().partition("/")
    if not owner or not sep or not name or "/" in name:
        msg = f"Invalid repository {repo!r}; expected 'owner/name'"
        raise ValueError(msg)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _redact_secrets(message: str, secrets: Sequence[str]) -> str:
    redacted = message
    for secret in secrets:
        cleaned = secret.strip()
        if cleaned:
            redacted = redacted.replace(cleaned, "[REDACTED]")
    return redacted


def _bridge_lane_runner(
    lane: str,
    task: str,
    *,
    repo: str = "",
    ledger: Ledger | None = None,
    extra_sources: list[dict[str, Any]] | None = None,
) -> object:
    return managed_agent.run_lane(lane, task, ledger, repo=repo or None, extra_sources=extra_sources)


class _LiveProofGitHubAdapter:
    """Adapts ``HttpxGitHubClient`` + GitHub REST endpoints to ``ProofHistoryClient`` and ``PullRequestClient``."""

    def __init__(
        self,
        github: GitHubClient,
        *,
        token: str = "",
        base_url: str = DEFAULT_API_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._github = github
        self._base_url = base_url.rstrip("/")
        self._owns_http = http_client is None and bool(token)
        self._http = (
            http_client
            if http_client is not None
            else (httpx.Client(headers=_github_headers(token), timeout=30.0) if token else None)
        )

    def close(self) -> None:
        """Close any internally owned ``httpx.Client``."""
        if self._owns_http and self._http is not None:
            self._http.close()

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        """Return UTF-8 file content at ``ref`` in ``repo`` (bridging ``ProofHistoryClient`` parameter order)."""
        return self._github.get_file_content(repo, path, ref)

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        """Fetch recent completed workflow job attempts for ``repo``."""
        if self._http is None:
            return ()
        repo_path = _repo_api_path(repo)
        runs_resp = self._http.get(
            f"{self._base_url}/repos/{repo_path}/actions/runs",
            params={"status": "completed", "per_page": str(_JOB_RUN_PAGE_SIZE)},
        )
        runs_resp.raise_for_status()
        runs_payload = runs_resp.json()
        workflow_runs = runs_payload.get("workflow_runs", []) if isinstance(runs_payload, dict) else []
        job_runs: list[JobRun] = []
        for run in workflow_runs:
            if not isinstance(run, dict):
                continue
            job_runs.extend(self._jobs_for_run(repo_path, run))
        return job_runs

    def _jobs_for_run(self, repo_path: str, run: dict[str, Any]) -> list[JobRun]:
        if self._http is None:
            return []
        run_id = run.get("id")
        if not isinstance(run_id, int):
            return []
        jobs_resp = self._http.get(
            f"{self._base_url}/repos/{repo_path}/actions/runs/{run_id}/jobs",
            params={"per_page": str(_JOBS_PER_RUN_LIMIT)},
        )
        jobs_resp.raise_for_status()
        jobs_data = jobs_resp.json()
        raw_jobs = jobs_data.get("jobs", []) if isinstance(jobs_data, dict) else []
        sha = str(run.get("head_sha") or "")
        branch = str(run.get("head_branch") or "main")
        created_at = str(run.get("created_at") or "")
        workflow_name = str(run.get("name") or "CI")
        attempt = int(run.get("run_attempt") or 1)
        parsed: list[JobRun] = []
        for job in raw_jobs:
            if not isinstance(job, dict):
                continue
            conclusion = job.get("conclusion")
            job_name = job.get("name")
            if isinstance(conclusion, str) and isinstance(job_name, str) and sha:
                parsed.append(
                    JobRun(
                        sha=sha,
                        branch=branch,
                        created_at=created_at,
                        workflow=workflow_name,
                        job=job_name,
                        attempt=attempt,
                        conclusion=conclusion,
                    )
                )
        return parsed

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        """Download MP4 recording artifact or fallback failed-job logs for ``run_id``."""
        if self._http is None:
            msg = "HTTP client is required to download run failure output"
            raise RuntimeError(msg)
        repo_path = _repo_api_path(repo)
        artifact_bytes = self._download_mp4_artifact(repo_path, run_id)
        if artifact_bytes is not None:
            return artifact_bytes
        return self._download_failed_job_logs(repo_path, run_id)

    def _download_mp4_artifact(self, repo_path: str, run_id: str) -> bytes | None:
        if self._http is None:
            return None
        resp = self._http.get(f"{self._base_url}/repos/{repo_path}/actions/runs/{quote(run_id, safe='')}/artifacts")
        if not resp.is_success:
            return None
        data = resp.json()
        artifacts = data.get("artifacts", []) if isinstance(data, dict) else []
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("expired"):
                continue
            artifact_id = artifact.get("id")
            if not isinstance(artifact_id, int):
                continue
            zip_resp = self._http.get(
                f"{self._base_url}/repos/{repo_path}/actions/artifacts/{artifact_id}/zip",
                follow_redirects=True,
            )
            if not zip_resp.is_success:
                continue
            mp4 = _extract_mp4_from_zip(zip_resp.content)
            if mp4 is not None:
                return mp4
        return None

    def _download_failed_job_logs(self, repo_path: str, run_id: str) -> bytes:
        assert self._http is not None  # noqa: S101
        jobs_resp = self._http.get(f"{self._base_url}/repos/{repo_path}/actions/runs/{quote(run_id, safe='')}/jobs")
        jobs_resp.raise_for_status()
        jobs_payload = jobs_resp.json()
        jobs = jobs_payload.get("jobs", []) if isinstance(jobs_payload, dict) else []
        chunks: list[bytes] = []
        for job in jobs:
            if not isinstance(job, dict) or job.get("conclusion") != "failure":
                continue
            job_id = job.get("id")
            if not isinstance(job_id, int):
                continue
            log_resp = self._http.get(
                f"{self._base_url}/repos/{repo_path}/actions/jobs/{job_id}/logs",
                follow_redirects=True,
            )
            if log_resp.is_success and log_resp.content:
                chunks.append(log_resp.content)
        if not chunks:
            msg = f"No failure output found for workflow run {run_id!r}"
            raise OSError(msg)
        return b"\n".join(chunks)

    def commit_files(self, repo: str, branch: str, files: Mapping[str, str | bytes], message: str) -> str:
        """Commit ``files`` atop ``branch``'s current tip SHA and return the new commit SHA."""
        if self._http is None:
            msg = "HTTP client is required to resolve branch tip SHA"
            raise RuntimeError(msg)
        repo_path = _repo_api_path(repo)
        ref_resp = self._http.get(f"{self._base_url}/repos/{repo_path}/git/ref/heads/{quote(branch, safe='/')}")
        ref_resp.raise_for_status()
        ref_data = ref_resp.json()
        tip_sha = str(ref_data["object"]["sha"])
        if isinstance(self._github, HttpxGitHubClient):
            return self._github.commit_files(repo, branch, tip_sha, files, message)
        text_files = {k: v if isinstance(v, str) else v.decode("utf-8", errors="replace") for k, v in files.items()}
        return self._github.commit_files(repo, branch, tip_sha, text_files, message)

    def open_pull_request(self, repo: str, *, head: str, base: str, title: str, body: str) -> str:
        """Open a pull request from ``head`` into ``base``."""
        return self._github.open_pull_request(repo, title=title, body=body, head=head, base=base)

    def list_run_artifacts(self, repo: str, run_id: int | str) -> list[RunArtifact]:
        """Forward artifact listing when the underlying client implements ``RunArtifactsClient``."""
        if isinstance(self._github, RunArtifactsClient):
            return self._github.list_run_artifacts(repo, run_id)
        return []

    def download_artifact(self, repo: str, artifact_id: int) -> bytes:
        """Forward artifact ZIP download when the underlying client implements ``RunArtifactsClient``."""
        if isinstance(self._github, RunArtifactsClient):
            return self._github.download_artifact(repo, artifact_id)
        msg = "Underlying GitHub client does not support download_artifact"
        raise RuntimeError(msg)

    def get_failed_job_logs(
        self,
        repo: str,
        run_id: int | str,
        *,
        check: str | None = None,
        max_bytes: int = DEFAULT_FAILED_LOG_MAX_BYTES,
    ) -> bytes:
        """Forward failed job log retrieval when the underlying client implements ``RunArtifactsClient``."""
        if isinstance(self._github, RunArtifactsClient):
            return self._github.get_failed_job_logs(repo, run_id, check=check, max_bytes=max_bytes)
        return self._download_failed_job_logs(_repo_api_path(repo), str(run_id))

    def list_failed_job_names(self, repo: str, run_id: int | str) -> list[str]:
        """Forward failed job names lookup when supported by ``HttpxGitHubClient``."""
        lister = getattr(self._github, "list_failed_job_names", None)
        if callable(lister):
            return [str(name) for name in lister(repo, run_id)]
        return []


def _extract_mp4_from_zip(raw_zip: bytes) -> bytes | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
            for member in archive.infolist():
                if not member.is_dir() and member.filename.lower().endswith(".mp4"):
                    return archive.read(member.filename)
    except zipfile.BadZipFile:
        return None
    return None


def add_runtime_subcommands(commands: Any) -> None:  # noqa: ANN401
    """Register ``run-cycle``, ``serve``, and ``register-webhook`` on an argparse subparsers object."""
    cycle = commands.add_parser("run-cycle", help="run an optimization cycle against a GitHub fork")
    cycle.add_argument("--repo", required=True, help="target repository slug (owner/name)")
    cycle.add_argument("--ledger", required=True, help="path to the SQLite ledger database")
    cycle.add_argument(
        "--timing-pairs", type=int, default=5, help="interleaved timing pairs per candidate (default: 5)"
    )
    cycle.add_argument("--parallel-runs", type=int, default=10, help="max concurrent CI workflow runs (default: 10)")
    cycle.add_argument(
        "--optimizer-fanout", type=int, default=1, help="Optimizer lanes run at once on top hypotheses (default: 1)"
    )
    cycle.add_argument("--until-sla", action="store_true", help="compound cycles until the CI SLA is met")
    cycle.add_argument("--max-cycles", type=int, default=5, help="max compounding cycles for --until-sla (default: 5)")
    cycle.add_argument("--workflow", default="ci.yml", help="target workflow file name (default: ci.yml)")
    cycle.add_argument("--workspace", help="optional local checkout path for analysis tools")
    cycle.add_argument("--no-open-pr", action="store_true", help="do not open pull requests for kept candidates")
    cycle.add_argument("--base-branch", default="main", help="base branch for opened pull requests (default: main)")

    serve = commands.add_parser("serve", help="start the FastAPI webhook & Pit Wall server")
    serve.add_argument(
        "--ledger",
        default=os.environ.get("PIT_CREW_LEDGER_PATH", DEFAULT_LEDGER_PATH),
        help="path to the SQLite ledger database (default: $PIT_CREW_LEDGER_PATH or .pit_crew/ledger.db)",
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    serve.add_argument("--parallel-runs", type=int, default=10, help="max concurrent CI workflow runs (default: 10)")
    serve.add_argument(
        "--optimizer-fanout", type=int, default=3, help="Optimizer lanes run at once on top hypotheses (default: 3)"
    )

    hook = commands.add_parser("register-webhook", help="idempotently configure the GitHub workflow_run webhook")
    hook.add_argument("--repo", required=True, help="target repository slug (owner/name)")
    hook.add_argument("--url", required=True, help="public HTTPS URL for POST /webhooks/github")
    hook.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="allow plain http:// webhook URLs for local test harnesses",
    )


def _build_live_dispatcher(
    ledger: SqliteLedger,
    tracker: RunTracker,
    config: DispatcherConfig,
    *,
    github: GitHubClient | None,
    stack: ExitStack,
) -> Dispatcher:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if github is not None:
        gh = github
    else:
        live_gh = HttpxGitHubClient(token=token)
        stack.callback(live_gh.close)
        gh = live_gh
    adapter = _LiveProofGitHubAdapter(gh, token=token)
    stack.callback(adapter.close)
    bridge = ProofBridge(adapter, run_lane=_bridge_lane_runner, ask=managed_agent.ask_multimodal)
    return Dispatcher(
        ledger,
        gh,
        config=config,
        run_tracker=tracker,
        use_host_workspace=True,
        proof_bridge=bridge,
    )


def _latest_evidence_for_verdict(ledger: Ledger, verdict: Verdict | None) -> Evidence | None:
    if verdict is None:
        return None
    matches = [ev for ev in ledger.all(Evidence) if ev.candidate_id == verdict.candidate_id]
    return matches[-1] if matches else None


def _ensure_workspace_dir(workspace: str | None) -> None:
    if workspace is not None and not Path(workspace).is_dir():
        msg = f"Workspace directory does not exist: {workspace!r}"
        raise FileNotFoundError(msg)


def _require_non_empty_env(value: str, env_name: str) -> None:
    if not value:
        msg = f"{env_name} environment variable is required"
        raise ValueError(msg)


def _run_cycle_command(
    args: argparse.Namespace,
    *,
    dispatcher: Any = None,  # noqa: ANN401
    github: GitHubClient | None = None,
) -> tuple[dict[str, Any], bool]:
    started = time.monotonic()
    summary: dict[str, Any] = {
        "command": "run-cycle",
        "repo": args.repo,
        "keep": False,
        "speedup": 0.0,
        "p_value": None,
        "median_seconds": 0.0,
        "p95_seconds": 0.0,
        "sla_met": False,
        "reason": "",
        "bugs_caught": 0,
        "bugs_total": 0,
    }
    try:
        ledger = SqliteLedger(args.ledger)
        _ensure_workspace_dir(args.workspace)
        with ExitStack() as stack:
            if dispatcher is not None:
                active_dispatcher = dispatcher
            else:
                config = DispatcherConfig(
                    timing_pairs=args.timing_pairs,
                    max_compounding_cycles=args.max_cycles,
                    workflow_file=args.workflow,
                    auto_open_pr=not args.no_open_pr,
                    base_branch=args.base_branch,
                    max_parallel_runs=args.parallel_runs,
                    optimizer_fanout=args.optimizer_fanout,
                )
                active_dispatcher = _build_live_dispatcher(
                    ledger,
                    RunTracker(),
                    config,
                    github=github,
                    stack=stack,
                )
            ws_ctx = host_workspace.use_workspace(args.workspace) if args.workspace is not None else nullcontext()
            with ws_ctx:
                if args.until_sla:
                    verdicts = active_dispatcher.run_until_sla(args.repo)
                    verdict: Verdict | None = verdicts[-1] if verdicts else None
                else:
                    verdict = active_dispatcher.run_cycle(args.repo)
        evidence = _latest_evidence_for_verdict(ledger, verdict)
    except Exception as exc:  # noqa: BLE001
        secrets = (os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GEMINI_API_KEY", ""))
        summary["error"] = _redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return summary, False

    if verdict is not None:
        summary.update(
            {
                "keep": verdict.keep,
                "speedup": verdict.speedup,
                "p_value": verdict.p_value,
                "median_seconds": verdict.median_seconds,
                "p95_seconds": verdict.p95_seconds,
                "sla_met": verdict.sla_met,
                "reason": verdict.reason,
                "bugs_caught": evidence.bugs_caught if evidence is not None else 0,
                "bugs_total": evidence.bugs_total if evidence is not None else 0,
                "pr_url": getattr(active_dispatcher, "last_opened_pr_url", None),
            }
        )
    else:
        summary["reason"] = "no verdict produced"
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return summary, verdict is not None


def _run_serve_command(
    args: argparse.Namespace,
    *,
    dispatcher: Any = None,  # noqa: ANN401
    github: GitHubClient | None = None,
    serve_runner: Callable[..., None] | None = None,
) -> tuple[dict[str, Any], bool]:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ledger = SqliteLedger(args.ledger)
    tracker = RunTracker()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    webhook_secret = os.environ.get("PIT_CREW_WEBHOOK_SECRET", "").strip() or None
    api_token = os.environ.get("PIT_CREW_API_TOKEN", "").strip() or None

    with ExitStack() as stack:
        if dispatcher is not None:
            active_dispatcher = dispatcher
        elif github is not None or github_token:
            config = DispatcherConfig(max_parallel_runs=args.parallel_runs, optimizer_fanout=args.optimizer_fanout)
            active_dispatcher = _build_live_dispatcher(
                ledger,
                tracker,
                config,
                github=github,
                stack=stack,
            )
        else:
            active_dispatcher = None

        app = create_app(
            ledger,
            dispatcher=active_dispatcher,
            webhook_secret=webhook_secret,
            run_tracker=tracker,
            api_token=api_token,
        )
        runner = serve_runner if serve_runner is not None else uvicorn.run
        runner(app, host=args.host, port=args.port)

    return {
        "command": "serve",
        "host": args.host,
        "port": args.port,
        "ledger": str(args.ledger),
        "dispatcher_enabled": active_dispatcher is not None,
    }, True


def _validate_webhook_url(raw_url: str, *, allow_insecure_http: bool) -> str:
    cleaned = raw_url.strip()
    parsed = urlparse(cleaned)
    allowed_schemes = {"https", "http"} if allow_insecure_http else {"https"}
    if parsed.scheme not in allowed_schemes or not parsed.netloc:
        msg = "Webhook URL must use https:// (pass --allow-insecure-http for local http:// testing)"
        raise ValueError(msg)
    return cleaned


def _upsert_github_webhook(
    client: httpx.Client,
    *,
    base_url: str,
    repo_path: str,
    target_url: str,
    secret: str,
) -> tuple[int, str]:
    hooks_resp = client.get(f"{base_url}/repos/{repo_path}/hooks", params={"per_page": "100"})
    hooks_resp.raise_for_status()
    hooks_data = hooks_resp.json()
    existing_id: int | None = None
    if isinstance(hooks_data, list):
        for hook in hooks_data:
            if not isinstance(hook, dict):
                continue
            cfg = hook.get("config")
            hook_id = hook.get("id")
            if isinstance(cfg, dict) and cfg.get("url") == target_url and isinstance(hook_id, int):
                existing_id = hook_id
                break

    hook_config = {
        "url": target_url,
        "content_type": "json",
        "secret": secret,
        "insecure_ssl": "0",
    }
    if existing_id is not None:
        resp = client.patch(
            f"{base_url}/repos/{repo_path}/hooks/{existing_id}",
            json={"active": True, "events": ["workflow_run"], "config": hook_config},
        )
        resp.raise_for_status()
        hook_id = existing_id
        action = "updated"
    else:
        resp = client.post(
            f"{base_url}/repos/{repo_path}/hooks",
            json={"name": "web", "active": True, "events": ["workflow_run"], "config": hook_config},
        )
        resp.raise_for_status()
        created = resp.json()
        hook_id = int(created["id"])
        action = "created"

    ping_resp = client.post(f"{base_url}/repos/{repo_path}/hooks/{hook_id}/pings")
    ping_resp.raise_for_status()
    return hook_id, action


def _run_register_webhook_command(
    args: argparse.Namespace,
    *,
    github: Any = None,  # noqa: ANN401
) -> tuple[dict[str, Any], bool]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    secret = os.environ.get("PIT_CREW_WEBHOOK_SECRET", "").strip()
    summary: dict[str, Any] = {
        "command": "register-webhook",
        "repo": args.repo,
        "url": args.url.strip(),
    }
    try:
        target_url = _validate_webhook_url(args.url, allow_insecure_http=bool(args.allow_insecure_http))
        repo_path = _repo_api_path(args.repo)
        _require_non_empty_env(secret, "PIT_CREW_WEBHOOK_SECRET")
        if isinstance(github, httpx.Client):
            hook_id, action = _upsert_github_webhook(
                github,
                base_url=str(github.base_url).rstrip("/") or DEFAULT_API_URL,
                repo_path=repo_path,
                target_url=target_url,
                secret=secret,
            )
        else:
            _require_non_empty_env(token, "GITHUB_TOKEN")
            base_url = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL).strip().rstrip("/")
            with httpx.Client(headers=_github_headers(token), timeout=30.0) as client:
                hook_id, action = _upsert_github_webhook(
                    client,
                    base_url=base_url,
                    repo_path=repo_path,
                    target_url=target_url,
                    secret=secret,
                )
    except Exception as exc:  # noqa: BLE001
        summary["error"] = _redact_secrets(f"{type(exc).__name__}: {exc}", (token, secret))
        return summary, False

    summary.update(
        {
            "url": target_url,
            "hook_id": hook_id,
            "action": action,
            "events": ["workflow_run"],
            "active": True,
            "pinged": True,
        }
    )
    return summary, True


def run_runtime_command(
    args: argparse.Namespace,
    *,
    dispatcher: Any = None,  # noqa: ANN401
    github: Any = None,  # noqa: ANN401
    serve_runner: Callable[..., None] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Execute ``run-cycle``, ``serve``, or ``register-webhook`` and return ``(summary, passed)``."""
    if args.command == "run-cycle":
        return _run_cycle_command(args, dispatcher=dispatcher, github=github)
    if args.command == "serve":
        return _run_serve_command(args, dispatcher=dispatcher, github=github, serve_runner=serve_runner)
    if args.command == "register-webhook":
        return _run_register_webhook_command(args, github=github)
    msg = f"Unsupported runtime command: {args.command!r}"
    raise ValueError(msg)


def _standalone_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pit_crew.runtime_cli", description="Pit Crew runtime commands")
    commands = parser.add_subparsers(dest="command", required=True)
    add_runtime_subcommands(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``runtime_cli`` standalone and return exit code ``0`` on success, ``1`` on failure."""
    args = _standalone_parser().parse_args(argv)
    summary, passed = run_runtime_command(args)
    sys.stdout.write(json.dumps(summary) + "\n")
    return 0 if passed else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
