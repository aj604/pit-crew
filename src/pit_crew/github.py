"""The GitHub operations Pit Crew's host code needs, as a Protocol so tests run offline.

A future ``HttpxGitHubClient`` implements this against the REST API (git refs, trees and commits);
tests use an in-memory fake. Nothing here talks to the network.
"""

from typing import Protocol


class CandidatePublisherClient(Protocol):
    """Writes a Candidate's files onto a branch of the target fork."""

    def reset_branch(self, repo: str, branch: str, sha: str) -> None:
        """Point ``refs/heads/{branch}`` in ``repo`` at ``sha``, creating it or force-resetting it."""
        ...

    def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        """Commit ``files`` (path to full content) atop ``branch``'s tip, advance the branch, return the new sha."""
        ...


class GitHubClient(CandidatePublisherClient, Protocol):
    """Every GitHub operation host code uses. Grows as the Dispatcher needs more (workflow runs, logs)."""
