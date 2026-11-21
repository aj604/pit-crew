"""Host-side GitHub boundary contract and ``httpx`` REST implementation.

Defines ``WorkflowRunResult``, the ``GitHubClient``, ``CandidatePublisherClient``,
and ``PullRequestClient`` protocols, and the concrete ``HttpxGitHubClient`` backed
by GitHub's REST and Git Data APIs. Keeping the client and its bearer token
strictly on the host allows the Dispatcher's orchestration, replay overlays,
evidence collection, and compounding loop to be developed and tested completely
offline without network calls or secrets.
"""

import base64
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable
from urllib.parse import quote

import httpx

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_DISPATCH_POLL_ATTEMPTS = 10
DEFAULT_DISPATCH_POLL_INTERVAL_SECONDS = 1.0
API_VERSION = "2022-11-28"
USER_AGENT = "pit-crew"
_DISPATCH_RUN_LOOKUP_LIMIT = 5
_HTTP_NO_CONTENT = 204
_HTTP_NOT_FOUND = 404
_HTTP_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_REPLAY_ARTIFACT_PREFIX = "pit-crew-replay-"
_FALLBACK_RECORDING_PREFIX = "failure-recordings-"


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
        files: dict[str, str],
        message: str,
    ) -> str:
        """Create a commit on ``branch`` atop ``base_sha`` with ``files``; returns new ``head_sha``."""
        repo_path = _repo_path(repo)
        base_commit = self._request_json("GET", f"/repos/{repo_path}/git/commits/{quote(base_sha, safe='')}")
        base_tree_sha = _require_str(_require_dict(base_commit, "tree"), "sha")

        if files:
            tree_entries = [
                {"path": path, "mode": "100644", "type": "blob", "content": content}
                for path, content in sorted(files.items())
            ]
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

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        """Download the replay recording artifact ZIP for workflow run ``run_id`` in ``repo``.

        Selects the newest non-expired ``pit-crew-replay-*`` artifact (falling back to
        ``failure-recordings-*``) and strips the ``Authorization`` header when following
        GitHub's 302 redirect to external blob storage. Raises ``FileNotFoundError`` when
        no non-expired recording artifact exists so replay verification fails closed.
        """
        repo_path = _repo_path(repo)
        normalized_run_id = quote(run_id.strip(), safe="")
        if not normalized_run_id:
            msg = "Workflow run_id must not be empty."
            raise ValueError(msg)
        artifacts_data = self._request_json("GET", f"/repos/{repo_path}/actions/runs/{normalized_run_id}/artifacts")
        artifact_id = _select_replay_artifact_id(artifacts_data)
        if artifact_id is None:
            msg = f"No replay recording artifact found for {repo} run {run_id}."
            raise FileNotFoundError(msg)
        return self._download_artifact_zip(repo_path, artifact_id)

    def _download_artifact_zip(self, repo_path: str, artifact_id: int) -> bytes:
        response = self._client.request(
            "GET",
            f"{self._base_url}/repos/{repo_path}/actions/artifacts/{artifact_id}/zip",
            headers=self._headers,
            follow_redirects=False,
        )
        if response.status_code in _HTTP_REDIRECT_CODES:
            location = response.headers.get("location") or response.headers.get("Location")
            if not location:
                msg = f"GitHub artifact {artifact_id} redirect is missing Location header."
                raise ValueError(msg)
            blob_headers = {k: v for k, v in self._headers.items() if k.lower() != "authorization"}
            blob_response = self._client.request("GET", location, headers=blob_headers, follow_redirects=True)
            blob_response.raise_for_status()
            return blob_response.content
        response.raise_for_status()
        return response.content

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


def _select_replay_artifact_id(artifacts_data: Mapping[str, object]) -> int | None:
    """Return the preferred non-expired replay recording artifact ID, or ``None``."""
    raw_artifacts = artifacts_data.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return None
    primary: list[int] = []
    fallback: list[int] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or item.get("expired") is True:
            continue
        name = _optional_str(item, "name") or ""
        artifact_id = item.get("id")
        if not isinstance(artifact_id, int) or isinstance(artifact_id, bool):
            continue
        if name.startswith(_REPLAY_ARTIFACT_PREFIX):
            primary.append(artifact_id)
        elif name.startswith(_FALLBACK_RECORDING_PREFIX):
            fallback.append(artifact_id)
    if primary:
        return primary[-1]
    return fallback[-1] if fallback else None


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
