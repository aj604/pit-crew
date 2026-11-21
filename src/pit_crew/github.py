"""The GitHub operations Pit Crew's host code needs, as a Protocol so tests run offline.

A future ``HttpxGitHubClient`` implements this against the REST API (git refs, trees and commits);
tests use an in-memory fake. Nothing here talks to the network.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pit_crew.lanes.proof.miner import JobRun


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


class ProofHistoryClient(Protocol):
    """Reads base files and recent workflow job runs for Proof pre-pass mining."""

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        """Return UTF-8 file content at ``ref`` in ``repo``, or ``None`` if absent."""
        ...

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        """Return recent GitHub Actions job run attempts for ``repo``."""
        ...

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        """Return failed job logs or recorded artifact bytes for workflow run ``run_id`` in ``repo``."""
        ...


class GitHubClient(CandidatePublisherClient, ProofHistoryClient, PullRequestClient, Protocol):
    """Every GitHub operation host code uses. Grows as the Dispatcher needs more (workflow runs, logs)."""
