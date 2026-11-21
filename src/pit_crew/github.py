"""Host-side GitHub boundary contract.

Defines ``WorkflowRunResult`` and the ``GitHubClient`` protocol so the Dispatcher's
orchestration, replay overlays, evidence collection, and compounding loop can be
developed and tested completely offline without network calls or secrets.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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
