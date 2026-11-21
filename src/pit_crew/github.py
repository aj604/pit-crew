"""Host-side GitHub boundary contract and ``httpx`` REST implementation.

Defines ``WorkflowRunResult``, the ``GitHubClient`` protocol, and the concrete
``HttpxGitHubClient`` backed by GitHub's REST and Git Data APIs. Keeping the
client and its bearer token strictly on the host allows the Dispatcher's
orchestration, replay overlays, evidence collection, and compounding loop to be
developed and tested completely offline without network calls or secrets.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable
from urllib.parse import quote

import httpx

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 30.0
API_VERSION = "2022-11-28"
USER_AGENT = "pit-crew"
_DISPATCH_RUN_LOOKUP_LIMIT = 5
_HTTP_NO_CONTENT = 204


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

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_API_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """Initialize the client with an explicit token or ``GITHUB_TOKEN`` from the host environment.

        Args:
            token: Personal access token or GitHub App token. Falls back to ``GITHUB_TOKEN``.
            base_url: GitHub REST API root URL.
            timeout: Request timeout in seconds when creating an internal ``httpx.Client``.
            transport: Optional custom ``httpx`` transport (e.g. ``httpx.MockTransport`` for tests).
            client: Optional pre-built ``httpx.Client`` managed by the caller.

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

        normalized_base_url = base_url.rstrip("/")
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
        workflow_path = quote(workflow, safe="")
        response = self._request(
            "POST",
            f"/repos/{repo_path}/actions/workflows/{workflow_path}/dispatches",
            json_body={"ref": ref, "return_run_details": True},
        )
        if response.status_code != _HTTP_NO_CONTENT and response.content:
            dispatch_data = _decode_json_object(response)
            if "workflow_run_id" in dispatch_data:
                return _require_int(dispatch_data, "workflow_run_id")

        runs_data = self._request_json(
            "GET",
            f"/repos/{repo_path}/actions/workflows/{workflow_path}/runs",
            params={
                "branch": ref,
                "event": "workflow_dispatch",
                "per_page": str(_DISPATCH_RUN_LOOKUP_LIMIT),
            },
        )
        runs = runs_data.get("workflow_runs")
        if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
            msg = f"No workflow_dispatch run found for {repo!r} workflow {workflow!r} on ref {ref!r}."
            raise RuntimeError(msg)
        return _require_int(runs[0], "id")

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
