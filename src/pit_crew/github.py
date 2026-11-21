"""Host-side GitHub boundary contract and ``httpx`` REST implementation.

Defines ``WorkflowRunResult``, ``RunArtifact``, ``RunJob``, the ``GitHubClient``,
``RunArtifactsClient``, ``CandidatePublisherClient``, and ``PullRequestClient`` protocols,
and the concrete ``HttpxGitHubClient`` backed by GitHub's REST and Git Data APIs. Keeping
the client and its bearer token strictly on the host allows the Dispatcher's orchestration,
replay overlays, evidence collection, and compounding loop to be developed and tested
completely offline without network calls or secrets.
"""

import base64
import io
import json
import os
import re
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable
from urllib.parse import quote, urljoin, urlparse

import httpx

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_DISPATCH_POLL_ATTEMPTS = 10
DEFAULT_DISPATCH_POLL_INTERVAL_SECONDS = 1.0
MAX_ARTIFACT_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB ZIP archive download cap
DEFAULT_FAILED_LOG_MAX_BYTES = 262_144  # 256 KiB cap for failed job log excerpts
API_VERSION = "2022-11-28"
USER_AGENT = "pit-crew"
_DISPATCH_RUN_LOOKUP_LIMIT = 5
_HTTP_NO_CONTENT = 204
_HTTP_NOT_FOUND = 404
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECT_HOPS = 3
_ARTIFACTS_PER_PAGE = "100"
_REPLAY_ARTIFACT_PREFIX = "pit-crew-replay-"
_FALLBACK_RECORDING_PREFIX = "failure-recordings-"
_REPLAY_META_FILENAME = "replay-meta.json"
_MAX_META_BYTES = 64 * 1024
_MATRIX_SUFFIX = re.compile(r"\s*\(.*\)$")
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TRAILING_INDEX_RE = re.compile(r"-\d+$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DispatchPollConfig:
    """Polling configuration for ``dispatch_workflow`` run-ID fallback lookups."""

    attempts: int = DEFAULT_DISPATCH_POLL_ATTEMPTS
    interval_seconds: float = DEFAULT_DISPATCH_POLL_INTERVAL_SECONDS
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = _utc_now

    def __post_init__(self) -> None:
        """Validate polling bounds."""
        if self.attempts < 1:
            msg = "Dispatch poll attempts must be at least 1."
            raise ValueError(msg)
        if self.interval_seconds < 0:
            msg = "Dispatch poll interval_seconds must be non-negative."
            raise ValueError(msg)


@dataclass(frozen=True)
class WorkflowRunResult:
    """Outcome and timing metadata of a single GitHub Actions workflow run."""

    run_id: int
    status: str  # "completed" | "in_progress" | "queued"
    conclusion: str | None  # "success" | "failure" | "cancelled" | ...
    duration_seconds: float
    html_url: str
    head_sha: str
    head_branch: str


@dataclass(frozen=True)
class RunArtifact:
    """Metadata for one GitHub Actions workflow run artifact."""

    id: int
    name: str
    size_in_bytes: int
    archive_download_url: str
    expired: bool
    created_at: str = ""


@dataclass(frozen=True)
class RunJob:
    """Outcome metadata for one job inside a GitHub Actions workflow run."""

    id: int
    name: str
    conclusion: str | None
    workflow_name: str = ""


@runtime_checkable
class RunArtifactsClient(Protocol):
    """Reads GitHub Actions run artifacts and failed job logs for multimodal Proof verification."""

    def list_run_artifacts(self, repo: str, run_id: int | str) -> list[RunArtifact]:
        """List artifacts uploaded by workflow run ``run_id`` in ``repo``."""
        ...

    def download_artifact(self, repo: str, artifact_id: int) -> bytes:
        """Download artifact ZIP archive bytes for ``artifact_id``, stripping auth on redirects."""
        ...

    def get_failed_job_logs(
        self,
        repo: str,
        run_id: int | str,
        *,
        check: str | None = None,
        max_bytes: int = DEFAULT_FAILED_LOG_MAX_BYTES,
    ) -> bytes:
        """Download logs for failed jobs in workflow run ``run_id``, capped at ``max_bytes``."""
        ...


@runtime_checkable
class CandidatePublisherClient(Protocol):
    """Writes a Candidate's files onto a branch of the target fork."""

    def reset_branch(self, repo: str, branch: str, sha: str) -> None:
        """Point ``refs/heads/{branch}`` in ``repo`` at ``sha``, creating it or force-resetting it."""
        ...

    def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        """Commit ``files`` (path to full content) atop ``branch``'s tip, advance the branch, return the new sha."""
        ...


@runtime_checkable
class PullRequestClient(Protocol):
    """Commits proof artifacts onto a candidate branch and opens a pull request."""

    def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        """Commit ``files`` (path to full content) atop ``branch``'s tip, advance the branch, return the new sha."""
        ...

    def open_pull_request(self, repo: str, *, head: str, base: str, title: str, body: str) -> str:
        """Open a pull request from ``head`` into ``base`` in ``repo`` and return its URL."""
        ...


@runtime_checkable
class GitHubClient(Protocol):
    """Protocol for host-side GitHub operations required by the Dispatcher."""

    def get_default_branch_sha(self, repo: str) -> str:
        """Return the current HEAD commit SHA of ``repo``'s default branch."""
        ...

    def create_branch(self, repo: str, branch: str, base_sha: str) -> str:
        """Create ``branch`` in ``repo`` pointing at ``base_sha`` and return the created SHA."""
        ...

    def delete_branch(self, repo: str, branch: str) -> None:
        """Delete ``branch`` from ``repo``."""
        ...

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        """Create a commit on ``branch`` atop ``base_sha`` with ``files``; returns new ``head_sha``."""
        ...

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        """Trigger workflow_dispatch (or empty-commit push fallback) and return ``run_id``."""
        ...

    def get_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
        """Fetch status, conclusion, and timing for workflow run ``run_id`` in ``repo``."""
        ...

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """Return UTF-8 text of ``path`` at ``ref`` in ``repo``, or ``None`` if absent."""
        ...

    def open_pull_request(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> str:
        """Open a PR and return its HTML URL."""
        ...


class HttpxGitHubClient:
    """Concrete ``GitHubClient`` using ``httpx`` and host-side bearer credentials."""

    def __init__(  # noqa: PLR0913
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_API_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
        dispatch_poll: DispatchPollConfig | None = None,
    ) -> None:
        """Initialize the client with an explicit token or ``GITHUB_TOKEN`` from the host environment.

        Args:
            token: Personal access token or GitHub App token. Falls back to ``GITHUB_TOKEN``.
            base_url: GitHub REST API root URL.
            timeout: Request timeout in seconds when creating an internal ``httpx.Client``.
            transport: Optional custom ``httpx`` transport (e.g. ``httpx.MockTransport`` for tests).
            client: Optional pre-built ``httpx.Client`` managed by the caller.
            dispatch_poll: Optional polling configuration for ``dispatch_workflow`` fallback lookups.

        Raises:
            ValueError: If no token is available, ``base_url`` is empty, or both ``client`` and
                ``transport`` are supplied.
        """
        if client is not None and transport is not None:
            msg = "Pass either 'client' or 'transport', not both."
            raise ValueError(msg)

        resolved_token = (token if token is not None else os.environ.get("GITHUB_TOKEN", "")).strip()
        if not resolved_token:
            msg = "GitHub token is required; pass 'token' or set GITHUB_TOKEN."
            raise ValueError(msg)

        normalized_base_url = base_url.strip().rstrip("/")
        if not normalized_base_url:
            msg = "GitHub API base_url must not be empty."
            raise ValueError(msg)

        self._base_url = normalized_base_url
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {resolved_token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.Client(
                timeout=timeout,
                transport=transport,
            )
        )
        self._dispatch_poll = dispatch_poll if dispatch_poll is not None else DispatchPollConfig()
        self._seen_dispatch_run_ids: dict[str, set[int]] = {}

    def close(self) -> None:
        """Close the underlying ``httpx.Client`` when owned by this instance."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        """Enter the context manager and return this client."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close owned HTTP resources when leaving the context manager."""
        self.close()

    def get_default_branch_sha(self, repo: str) -> str:
        """Return the current HEAD commit SHA of ``repo``'s default branch."""
        repo_path = _repo_path(repo)
        repo_data = self._request_json("GET", f"/repos/{repo_path}")
        default_branch = _require_str(repo_data, "default_branch")
        ref_data = self._request_json("GET", f"/repos/{repo_path}/git/ref/heads/{_branch_path(default_branch)}")
        return _require_str(_require_dict(ref_data, "object"), "sha")

    def create_branch(self, repo: str, branch: str, base_sha: str) -> str:
        """Create ``branch`` in ``repo`` pointing at ``base_sha`` and return the created SHA."""
        repo_path = _repo_path(repo)
        ref_data = self._request_json(
            "POST",
            f"/repos/{repo_path}/git/refs",
            json_body={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        return _require_str(_require_dict(ref_data, "object"), "sha")

    def delete_branch(self, repo: str, branch: str) -> None:
        """Delete ``branch`` from ``repo``."""
        repo_path = _repo_path(repo)
        self._request("DELETE", f"/repos/{repo_path}/git/refs/heads/{_branch_path(branch)}")

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: Mapping[str, str | bytes],
        message: str,
    ) -> str:
        """Create a commit on ``branch`` atop ``base_sha`` with text or binary ``files``; returns new ``head_sha``."""
        repo_path = _repo_path(repo)
        base_commit = self._request_json("GET", f"/repos/{repo_path}/git/commits/{quote(base_sha, safe='')}")
        base_tree_sha = _require_str(_require_dict(base_commit, "tree"), "sha")

        if files:
            tree_entries = [self._build_tree_entry(repo_path, path, content) for path, content in sorted(files.items())]
            created_tree = self._request_json(
                "POST",
                f"/repos/{repo_path}/git/trees",
                json_body={"base_tree": base_tree_sha, "tree": tree_entries},
            )
            tree_sha = _require_str(created_tree, "sha")
        else:
            tree_sha = base_tree_sha

        created_commit = self._request_json(
            "POST",
            f"/repos/{repo_path}/git/commits",
            json_body={"message": message, "tree": tree_sha, "parents": [base_sha]},
        )
        new_commit_sha = _require_str(created_commit, "sha")

        self._request(
            "PATCH",
            f"/repos/{repo_path}/git/refs/heads/{_branch_path(branch)}",
            json_body={"sha": new_commit_sha},
        )
        return new_commit_sha

    def _build_tree_entry(self, repo_path: str, path: str, content: str | bytes) -> dict[str, str]:
        if isinstance(content, bytes):
            created_blob = self._request_json(
                "POST",
                f"/repos/{repo_path}/git/blobs",
                json_body={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
            )
            return {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": _require_str(created_blob, "sha"),
            }
        return {"path": path, "mode": "100644", "type": "blob", "content": content}

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        """Trigger ``workflow_dispatch`` for ``workflow`` on ``ref`` and return the ``run_id``."""
        repo_path = _repo_path(repo)
        workflow_path = _workflow_path(workflow)
        cutoff = _to_utc(self._dispatch_poll.clock()).replace(microsecond=0)
        response = self._request(
            "POST",
            f"/repos/{repo_path}/actions/workflows/{workflow_path}/dispatches",
            json_body={"ref": ref, "return_run_details": True},
        )
        if response.status_code != _HTTP_NO_CONTENT and response.content:
            dispatch_data = _decode_json_object(response)
            if "workflow_run_id" in dispatch_data:
                run_id = _require_int(dispatch_data, "workflow_run_id")
                self._seen_dispatch_run_ids.setdefault(repo_path, set()).add(run_id)
                return run_id

        polled_run_id = self._poll_dispatched_run_id(
            repo_path=repo_path,
            workflow_path=workflow_path,
            branch=ref.removeprefix("refs/heads/"),
            cutoff=cutoff,
        )
        if polled_run_id is None:
            msg = f"No workflow_dispatch run found for {repo!r} workflow {workflow!r} on ref {ref!r}."
            raise RuntimeError(msg)
        return polled_run_id

    def _poll_dispatched_run_id(
        self,
        *,
        repo_path: str,
        workflow_path: str,
        branch: str,
        cutoff: datetime,
    ) -> int | None:
        seen_run_ids = self._seen_dispatch_run_ids.setdefault(repo_path, set())
        params = {
            "branch": branch,
            "event": "workflow_dispatch",
            "per_page": str(_DISPATCH_RUN_LOOKUP_LIMIT),
        }
        for attempt in range(self._dispatch_poll.attempts):
            runs_data = self._request_json(
                "GET",
                f"/repos/{repo_path}/actions/workflows/{workflow_path}/runs",
                params=params,
            )
            run_id = _find_dispatched_run_id(runs_data, cutoff=cutoff, seen_run_ids=seen_run_ids)
            if run_id is not None:
                seen_run_ids.add(run_id)
                return run_id
            if attempt + 1 < self._dispatch_poll.attempts:
                self._dispatch_poll.sleep(self._dispatch_poll.interval_seconds)
        return None

    def get_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
        """Fetch status, conclusion, and timing for workflow run ``run_id`` in ``repo``."""
        repo_path = _repo_path(repo)
        data = self._request_json("GET", f"/repos/{repo_path}/actions/runs/{run_id}")
        status = _require_str(data, "status")
        conclusion = _optional_str(data, "conclusion")
        return WorkflowRunResult(
            run_id=_require_int(data, "id"),
            status=status,
            conclusion=conclusion,
            duration_seconds=_compute_duration_seconds(data, status),
            html_url=_require_str(data, "html_url"),
            head_sha=_require_str(data, "head_sha"),
            head_branch=_optional_str(data, "head_branch") or "",
        )

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        """Return UTF-8 text of ``path`` at ``ref`` in ``repo``, or ``None`` if absent."""
        repo_path = _repo_path(repo)
        content_path = _content_path(path)
        response = self._client.request(
            "GET",
            f"{self._base_url}/repos/{repo_path}/contents/{content_path}",
            headers=self._headers,
            params={"ref": ref},
        )
        if response.status_code == _HTTP_NOT_FOUND:
            return None
        response.raise_for_status()
        data = _decode_json_object(response)
        raw_content = _require_str(data, "content")
        encoding = _optional_str(data, "encoding")
        if encoding is None or encoding == "none":
            return raw_content
        if encoding != "base64":
            msg = f"Unsupported content encoding {encoding!r} for {repo}:{path}@{ref}."
            raise ValueError(msg)
        return base64.b64decode(raw_content).decode("utf-8")

    def open_pull_request(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> str:
        """Open a PR and return its HTML URL."""
        repo_path = _repo_path(repo)
        pr_data = self._request_json(
            "POST",
            f"/repos/{repo_path}/pulls",
            json_body={"title": title, "body": body, "head": head, "base": base},
        )
        return _require_str(pr_data, "html_url")

    def list_run_artifacts(self, repo: str, run_id: int | str) -> list[RunArtifact]:
        """List GitHub Actions artifacts uploaded by workflow run ``run_id`` in ``repo``."""
        repo_path = _repo_path(repo)
        run_segment = quote(str(run_id), safe="")
        data = self._request_json(
            "GET",
            f"/repos/{repo_path}/actions/runs/{run_segment}/artifacts",
            params={"per_page": _ARTIFACTS_PER_PAGE},
        )
        raw_artifacts = data.get("artifacts")
        if not isinstance(raw_artifacts, list):
            return []
        artifacts: list[RunArtifact] = []
        for item in raw_artifacts:
            if not isinstance(item, dict):
                continue
            artifacts.append(
                RunArtifact(
                    id=_require_int(item, "id"),
                    name=_require_str(item, "name"),
                    size_in_bytes=_require_int(item, "size_in_bytes"),
                    archive_download_url=_optional_str(item, "archive_download_url") or "",
                    expired=bool(item.get("expired", False)),
                    created_at=_optional_str(item, "created_at") or "",
                )
            )
        return artifacts

    def download_artifact(
        self,
        repo: str,
        artifact_id: int,
        *,
        max_bytes: int = MAX_ARTIFACT_DOWNLOAD_BYTES,
    ) -> bytes:
        """Download artifact ZIP bytes for ``artifact_id``, stripping ``Authorization`` on 302 redirects."""
        repo_path = _repo_path(repo)
        url = f"{self._base_url}/repos/{repo_path}/actions/artifacts/{artifact_id}/zip"
        return self._download_with_stripped_redirect(url, max_bytes=max_bytes)

    def list_run_jobs(self, repo: str, run_id: int | str) -> list[RunJob]:
        """Return every job belonging to workflow run ``run_id`` in ``repo``."""
        repo_path = _repo_path(repo)
        run_segment = quote(str(run_id).strip(), safe="")
        data = self._request_json(
            "GET",
            f"/repos/{repo_path}/actions/runs/{run_segment}/jobs",
            params={"per_page": _ARTIFACTS_PER_PAGE},
        )
        raw_jobs = data.get("jobs")
        if not isinstance(raw_jobs, list):
            return []
        jobs: list[RunJob] = []
        for job in raw_jobs:
            if not isinstance(job, dict):
                continue
            jobs.append(
                RunJob(
                    id=_require_int(job, "id"),
                    name=_require_str(job, "name"),
                    conclusion=_optional_str(job, "conclusion"),
                    workflow_name=_optional_str(job, "workflow_name") or "",
                )
            )
        return jobs

    def get_run_failure_output(
        self,
        repo: str,
        run_id: str,
        *,
        check: str | None = None,
        expected_checks: Sequence[str] = (),
        check_aliases: Sequence[str] = (),
    ) -> bytes:
        """Download the replay recording artifact ZIP for workflow run ``run_id`` in ``repo``.

        When ``check`` or ``expected_checks`` is provided, resolves the expected check against
        the completed run's jobs (including matrix display names and ``check_aliases``), requires
        a matching job to have failed, and selects only an unambiguous recording artifact for that
        job (verifying ``replay-meta.json`` when present). Missing, non-matching, or ambiguous
        recordings raise ``FileNotFoundError`` so verification fails closed.
        """
        repo_path = _repo_path(repo)
        normalized_run_id = quote(run_id.strip(), safe="")
        if not normalized_run_id:
            msg = "Workflow run_id must not be empty."
            raise ValueError(msg)

        targets = _normalize_target_checks(check, expected_checks)
        artifacts_data = self._request_json(
            "GET",
            f"/repos/{repo_path}/actions/runs/{normalized_run_id}/artifacts",
            params={"per_page": _ARTIFACTS_PER_PAGE},
        )
        primary, fallback = _collect_replay_artifacts(artifacts_data)
        if not primary and not fallback:
            msg = f"No replay recording artifact found for {repo} run {run_id}."
            raise FileNotFoundError(msg)

        if not targets:
            artifact_id = _select_unambiguous_artifact_id(primary or fallback, repo=repo, run_id=run_id)
            return self.download_artifact(repo, artifact_id)

        jobs = self.list_run_jobs(repo, normalized_run_id)
        failed_resolved = _resolve_failed_jobs_for_checks(jobs, targets, check_aliases)
        if not failed_resolved:
            msg = f"Expected check(s) {', '.join(targets)!r} did not fail in {repo} run {run_id}."
            raise FileNotFoundError(msg)

        return self._download_matching_replay_artifact(
            repo=repo,
            run_id=run_id,
            primary=primary,
            fallback=fallback,
            resolved_jobs=failed_resolved,
            targets=targets,
            check_aliases=check_aliases,
        )

    def _download_matching_replay_artifact(  # noqa: PLR0913
        self,
        *,
        repo: str,
        run_id: str,
        primary: list[tuple[int, str]],
        fallback: list[tuple[int, str]],
        resolved_jobs: list[RunJob],
        targets: list[str],
        check_aliases: Sequence[str],
    ) -> bytes:
        matched_primary = _filter_artifacts_for_jobs(primary, resolved_jobs, targets, check_aliases)
        matched_fallback = _filter_artifacts_for_jobs(fallback, resolved_jobs, targets, check_aliases)
        if not matched_primary and not matched_fallback:
            msg = f"No replay recording artifact matching {', '.join(targets)!r} found for {repo} run {run_id}."
            raise FileNotFoundError(msg)

        if matched_primary:
            payload = self._resolve_artifact_pool(
                repo=repo,
                run_id=run_id,
                matched=matched_primary,
                resolved_jobs=resolved_jobs,
                targets=targets,
                check_aliases=check_aliases,
                allow_mismatch_fallback=bool(matched_fallback),
            )
            if payload is not None:
                return payload

        payload = self._resolve_artifact_pool(
            repo=repo,
            run_id=run_id,
            matched=matched_fallback,
            resolved_jobs=resolved_jobs,
            targets=targets,
            check_aliases=check_aliases,
            allow_mismatch_fallback=False,
        )
        assert payload is not None  # noqa: S101  allow_mismatch_fallback=False always returns or raises
        return payload

    def _resolve_artifact_pool(  # noqa: PLR0913
        self,
        *,
        repo: str,
        run_id: str,
        matched: list[tuple[int, str]],
        resolved_jobs: list[RunJob],
        targets: list[str],
        check_aliases: Sequence[str],
        allow_mismatch_fallback: bool,
    ) -> bytes | None:
        # Deduplicate repeated uploads of the exact same artifact name by keeping the newest ID.
        by_name = {name: artifact_id for artifact_id, name in matched}
        if len(by_name) == 1:
            payload = self.download_artifact(repo, next(iter(by_name.values())))
            if not _zip_metadata_matches_jobs(payload, resolved_jobs, targets, check_aliases):
                if allow_mismatch_fallback:
                    return None
                msg = f"Replay recording artifact metadata did not match {', '.join(targets)!r} in {repo} run {run_id}."
                raise FileNotFoundError(msg)
            return payload

        # Multiple distinct artifact names matched (e.g. legacy matrix slugs without display names):
        # disambiguate strictly via replay-meta.json, or fail closed as ambiguous.
        verified_payloads: list[bytes] = []
        for artifact_id in by_name.values():
            payload = self.download_artifact(repo, artifact_id)
            meta = _read_zip_replay_meta(payload)
            if meta is not None and _meta_matches_resolved_jobs(meta, resolved_jobs):
                verified_payloads.append(payload)
        if len(verified_payloads) == 1:
            return verified_payloads[0]
        if not verified_payloads and allow_mismatch_fallback:
            return None
        msg = (
            f"Ambiguous replay recording artifacts ({', '.join(sorted(by_name))}) "
            f"for {', '.join(targets)!r} in {repo} run {run_id}."
        )
        raise FileNotFoundError(msg)

    def get_failed_job_logs(  # noqa: PLR0913
        self,
        repo: str,
        run_id: int | str,
        *,
        check: str | None = None,
        expected_checks: Sequence[str] = (),
        check_aliases: Sequence[str] = (),
        max_bytes: int = DEFAULT_FAILED_LOG_MAX_BYTES,
    ) -> bytes:
        """Download logs for failed jobs in ``run_id`` (auth-stripped redirects), capped at ``max_bytes``."""
        jobs = self.list_run_jobs(repo, run_id)
        if not any(job.conclusion == "failure" for job in jobs):
            return b""
        targets = _normalize_target_checks(check, expected_checks)
        selected = (
            _resolve_failed_jobs_for_checks(jobs, targets, check_aliases)
            if targets
            else [job for job in jobs if job.conclusion == "failure"]
        )
        repo_path = _repo_path(repo)
        sections: list[bytes] = []
        for job in selected:
            url = f"{self._base_url}/repos/{repo_path}/actions/jobs/{job.id}/logs"
            raw_log = self._download_with_stripped_redirect(url, max_bytes=max_bytes, allow_not_found=True)
            if raw_log:
                sections.append(f"=== {job.name} ===\n".encode() + raw_log)
        combined = b"\n".join(sections)
        if len(combined) > max_bytes:
            return combined[-max_bytes:]
        return combined

    def list_failed_job_names(self, repo: str, run_id: int | str) -> list[str]:
        """Return display names of jobs whose conclusion is ``failure`` in ``run_id``."""
        return [job_name for _, job_name in self._list_failed_jobs(repo, run_id)]

    def _list_failed_jobs(self, repo: str, run_id: int | str) -> list[tuple[int, str]]:
        return [(job.id, job.name) for job in self.list_run_jobs(repo, run_id) if job.conclusion == "failure"]

    def _download_with_stripped_redirect(
        self,
        url: str,
        *,
        max_bytes: int,
        allow_not_found: bool = False,
    ) -> bytes:
        current_url = url
        headers: dict[str, str] = dict(self._headers)
        for _ in range(_MAX_REDIRECT_HOPS + 1):
            response = self._client.request(
                "GET",
                current_url,
                headers=headers,
                follow_redirects=False,
            )
            if allow_not_found and response.status_code == _HTTP_NOT_FOUND:
                return b""
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("location", "").strip()
                if not location:
                    msg = f"Redirect response from {current_url!r} is missing Location header."
                    raise ValueError(msg)
                next_url = urljoin(current_url, location)
                _require_http_url(next_url)
                current_url = next_url
                # Never forward GitHub bearer credentials to external blob storage redirects.
                headers = {"User-Agent": USER_AGENT}
                continue
            response.raise_for_status()
            body = response.content
            if len(body) > max_bytes:
                msg = f"Download from {current_url!r} exceeded {max_bytes}-byte limit ({len(body)} bytes)."
                raise ValueError(msg)
            return body
        msg = f"Too many redirects while downloading {url!r}."
        raise ValueError(msg)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        response = self._client.request(
            method,
            f"{self._base_url}{path}",
            headers=self._headers,
            json=json_body,
            params=params,
        )
        response.raise_for_status()
        return response

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        response = self._request(method, path, json_body=json_body, params=params)
        return _decode_json_object(response)


def _repo_path(repo: str) -> str:
    owner, sep, name = repo.partition("/")
    if not owner or not sep or not name or "/" in name:
        msg = f"Invalid repository {repo!r}; expected 'owner/name'."
        raise ValueError(msg)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _branch_path(branch: str) -> str:
    return quote(branch, safe="/")


def _content_path(path: str) -> str:
    stripped = path.strip("/")
    parts = stripped.split("/")
    if not stripped or "\\" in path or any(part in {"", ".", ".."} for part in parts):
        msg = f"Invalid repository file path {path!r}."
        raise ValueError(msg)
    return "/".join(quote(part, safe="") for part in parts)


def _workflow_path(workflow: str) -> str:
    normalized = workflow.strip().removeprefix("./").lstrip("/").removeprefix(".github/workflows/")
    if not normalized:
        msg = f"Invalid workflow identifier {workflow!r}."
        raise ValueError(msg)
    return quote(normalized, safe="")


def _find_dispatched_run_id(
    runs_data: Mapping[str, object],
    *,
    cutoff: datetime,
    seen_run_ids: set[int],
) -> int | None:
    runs = runs_data.get("workflow_runs")
    if not isinstance(runs, list):
        return None
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = _require_int(run, "id")
        if run_id in seen_run_ids:
            continue
        created_at = _parse_timestamp(_optional_str(run, "created_at"))
        if created_at is not None and _to_utc(created_at) >= cutoff:
            return run_id
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decode_json_object(response: httpx.Response) -> dict[str, object]:
    payload: object = response.json()
    if not isinstance(payload, dict):
        msg = f"Expected JSON object from GitHub API, got {type(payload).__name__}."
        raise TypeError(msg)
    return payload


def _require_dict(data: Mapping[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        msg = f"Expected JSON object for field {key!r}."
        raise TypeError(msg)
    return value


def _require_str(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        msg = f"Expected string for field {key!r}."
        raise TypeError(msg)
    return value


def _optional_str(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        msg = f"Expected string or null for field {key!r}."
        raise TypeError(msg)
    return value


def _require_int(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"Expected integer for field {key!r}."
        raise TypeError(msg)
    return value


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _normalize_target_checks(check: str | None, expected_checks: Sequence[str]) -> list[str]:
    targets = [item.strip() for item in expected_checks if item.strip()]
    if targets:
        return list(dict.fromkeys(targets))
    if check and check.strip():
        return [check.strip()]
    return []


def _collect_replay_artifacts(
    artifacts_data: Mapping[str, object],
) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """Return non-expired primary (``pit-crew-replay-*``) and fallback (``failure-recordings-*``) artifacts."""
    raw_artifacts = artifacts_data.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return [], []
    primary: list[tuple[int, str]] = []
    fallback: list[tuple[int, str]] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or item.get("expired") is True:
            continue
        name = _optional_str(item, "name") or ""
        artifact_id = item.get("id")
        if not isinstance(artifact_id, int) or isinstance(artifact_id, bool):
            continue
        if name.startswith(_REPLAY_ARTIFACT_PREFIX):
            primary.append((artifact_id, name))
        elif name.startswith(_FALLBACK_RECORDING_PREFIX):
            fallback.append((artifact_id, name))
    return primary, fallback


def _select_unambiguous_artifact_id(candidates: list[tuple[int, str]], *, repo: str, run_id: str) -> int:
    by_name = {name: artifact_id for artifact_id, name in candidates}
    if len(by_name) > 1:
        msg = (
            f"Ambiguous replay recording artifacts ({', '.join(sorted(by_name))}) "
            f"for {repo} run {run_id}; specify an expected check."
        )
        raise FileNotFoundError(msg)
    return next(iter(by_name.values()))


def _resolve_failed_jobs_for_checks(
    jobs: Sequence[RunJob],
    targets: Sequence[str],
    check_aliases: Sequence[str],
) -> list[RunJob]:
    """Resolve each target/alias independently against ``jobs`` and return deduplicated failed matches.

    Exact matches take precedence over folded matrix-parent matches per target so a passing
    exact check never hides another expected check's failing matrix leg, while a specific
    matrix leg that passed is never masked by another failing leg.
    """
    all_targets = [item.strip() for item in (*targets, *check_aliases) if item.strip()]
    seen_ids: set[int] = set()
    failed: list[RunJob] = []
    for target in all_targets:
        exact = [job for job in jobs if _job_matches_target(job, target, allow_fold=False)]
        matched = exact or [job for job in jobs if _job_matches_target(job, target, allow_fold=True)]
        for job in matched:
            if job.conclusion == "failure" and job.id not in seen_ids:
                seen_ids.add(job.id)
                failed.append(job)
    return failed


def _job_matches_target(job: RunJob, target: str, *, allow_fold: bool) -> bool:
    target_low = target.strip().lower()
    job_low = job.name.strip().lower()
    wf_low = job.workflow_name.strip().lower()
    if _exact_job_match(target_low, job_low, wf_low):
        return True
    if not allow_fold or _MATRIX_SUFFIX.search(target_low):
        return False
    return _exact_job_match(target_low, _fold_job_name(job_low), wf_low)


def _exact_job_match(target_low: str, job_low: str, wf_low: str) -> bool:
    if target_low == job_low:
        return True
    if wf_low and target_low == f"{wf_low}/{job_low}":
        return True
    if target_low.endswith(f"/{job_low}"):
        prefix = target_low[: -(len(job_low) + 1)].strip()
        if prefix and prefix.count("(") == prefix.count(")") and (not wf_low or prefix == wf_low):
            return True
    return False


def _strip_workflow_prefix(name: str) -> str:
    stripped = name.strip()
    match = _MATRIX_SUFFIX.search(stripped)
    if match is None:
        return stripped.rsplit("/", maxsplit=1)[-1].strip()
    head = stripped[: match.start()]
    suffix = stripped[match.start() :]
    return f"{head.rsplit('/', maxsplit=1)[-1].strip()}{suffix}"


def _filter_artifacts_for_jobs(
    candidates: list[tuple[int, str]],
    resolved_jobs: Sequence[RunJob],
    targets: Sequence[str],
    check_aliases: Sequence[str],
) -> list[tuple[int, str]]:
    display_slugs = {_slugify(job.name) for job in resolved_jobs if _slugify(job.name)}
    exact_matches = [item for item in candidates if _artifact_matches_slugs(item[1], display_slugs, exact_display=True)]
    if exact_matches:
        return exact_matches

    alias_slugs = set(display_slugs)
    for raw in (*targets, *check_aliases):
        bare = _strip_workflow_prefix(raw)
        slug = _slugify(bare)
        if slug:
            alias_slugs.add(slug)
        if not _MATRIX_SUFFIX.search(bare):
            folded_slug = _slugify(_fold_job_name(bare))
            if folded_slug:
                alias_slugs.add(folded_slug)
    for job in resolved_jobs:
        folded = _slugify(_fold_job_name(job.name))
        if folded:
            alias_slugs.add(folded)
    return [item for item in candidates if _artifact_matches_slugs(item[1], alias_slugs, exact_display=False)]


def _artifact_matches_slugs(artifact_name: str, slugs: set[str], *, exact_display: bool) -> bool:
    body = _strip_artifact_envelope(artifact_name)
    if not body:
        return False
    if body in slugs:
        return True
    if exact_display:
        return any(body.endswith(f"-{slug}") for slug in slugs)
    return any(body.startswith(f"{slug}-") or body.endswith(f"-{slug}") for slug in slugs)


def _strip_artifact_envelope(artifact_name: str) -> str:
    lower = artifact_name.lower()
    if lower.startswith(_REPLAY_ARTIFACT_PREFIX):
        stem = lower.removeprefix(_REPLAY_ARTIFACT_PREFIX)
    elif lower.startswith(_FALLBACK_RECORDING_PREFIX):
        stem = lower.removeprefix(_FALLBACK_RECORDING_PREFIX)
    else:
        return ""
    return _TRAILING_INDEX_RE.sub("", stem).strip("-")


def _slugify(value: str) -> str:
    return _SAFE_SLUG_RE.sub("-", value.strip().lower()).strip("-")[:80].strip("-")


def _read_zip_replay_meta(payload: bytes) -> dict[str, str] | None:
    if not payload.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.filename.rsplit("/", maxsplit=1)[-1].lower() != _REPLAY_META_FILENAME:
                    continue
                if info.file_size > _MAX_META_BYTES:
                    return None
                parsed = json.loads(archive.read(info.filename).decode("utf-8"))
                if isinstance(parsed, dict):
                    return {str(k): str(v) for k, v in parsed.items() if isinstance(k, str) and isinstance(v, str)}
    except (zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, OSError):
        return None
    return None


def _meta_matches_resolved_jobs(meta: Mapping[str, str], resolved_jobs: Sequence[RunJob]) -> bool:
    meta_name = meta.get("job_name", "").strip().lower()
    meta_wf = meta.get("workflow", "").strip().lower()
    if not meta_name:
        return False
    for job in resolved_jobs:
        if job.name.strip().lower() == meta_name and (
            not meta_wf or not job.workflow_name or job.workflow_name.strip().lower() == meta_wf
        ):
            return True
    return False


def _zip_metadata_matches_jobs(
    payload: bytes,
    resolved_jobs: Sequence[RunJob],
    targets: Sequence[str],
    check_aliases: Sequence[str],
) -> bool:
    meta = _read_zip_replay_meta(payload)
    if meta is None:
        return True
    if _meta_matches_resolved_jobs(meta, resolved_jobs):
        return True
    meta_name = meta.get("job_name", "").strip()
    meta_wf = meta.get("workflow", "").strip()
    if not meta_name:
        return True
    probe = RunJob(id=0, name=meta_name, conclusion="failure", workflow_name=meta_wf)
    return bool(_resolve_failed_jobs_for_checks([probe], targets, check_aliases))


def _require_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = f"Refusing redirect to non-HTTP(S) URL: {url!r}."
        raise ValueError(msg)


def _filter_jobs_for_check(
    failed_jobs: list[tuple[int, str]],
    check: str | None,
    *,
    expected_checks: Sequence[str] = (),
    check_aliases: Sequence[str] = (),
) -> list[tuple[int, str]]:
    targets = _normalize_target_checks(check, expected_checks)
    if not targets:
        return failed_jobs
    run_jobs = [RunJob(id=job_id, name=name, conclusion="failure") for job_id, name in failed_jobs]
    matched = _resolve_failed_jobs_for_checks(run_jobs, targets, check_aliases)
    return [(job.id, job.name) for job in matched]


def _fold_job_name(name: str) -> str:
    return _MATRIX_SUFFIX.sub("", name.strip()).strip().lower()


def _compute_duration_seconds(data: Mapping[str, object], status: str) -> float:
    run_started_raw = _optional_str(data, "run_started_at")
    if status == "queued" and not run_started_raw:
        return 0.0
    start_raw = run_started_raw or _optional_str(data, "created_at")
    end_raw = _optional_str(data, "updated_at")
    start = _parse_timestamp(start_raw)
    end = _parse_timestamp(end_raw)
    if start is None or end is None:
        return 0.0
    return max((end - start).total_seconds(), 0.0)
