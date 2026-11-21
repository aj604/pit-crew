"""Publishes an Optimizer Candidate onto its fork branch so timing runs have a real commit to measure.

Optimizer returns ``head_sha=""`` plus a ``files`` payload. The host checks every path against
``contracts.is_allowed_candidate_path`` before touching GitHub, then resets ``candidate.branch`` to the
resolved base, commits the files, and records the published Candidate (same id) in the ledger.
"""

from dataclasses import replace

from pit_crew.contracts import Candidate, is_allowed_candidate_path
from pit_crew.github import CandidatePublisherClient
from pit_crew.ledger import Ledger


class DisallowedCandidatePathError(ValueError):
    """Raised when a Candidate touches paths Optimizer may not modify. Nothing was published."""

    def __init__(self, candidate_id: str, paths: tuple[str, ...]) -> None:
        """Record which candidate and which paths were rejected."""
        self.candidate_id = candidate_id
        self.paths = paths
        super().__init__(f"Candidate {candidate_id} touches disallowed paths: {', '.join(paths)}")


class UnpublishableCandidateError(ValueError):
    """Raised when a Candidate lacks what publishing needs (repo, branch, base sha, or files)."""

    @classmethod
    def missing(cls, candidate_id: str, what: str) -> "UnpublishableCandidateError":
        """Build error naming the missing field."""
        return cls(f"Candidate {candidate_id} cannot be published: missing {what}")


def publish_candidate(
    client: CandidatePublisherClient,
    ledger: Ledger,
    candidate: Candidate,
    resolved_base_sha: str,
) -> Candidate:
    """Commit ``candidate.files`` onto ``candidate.branch`` atop ``resolved_base_sha`` and persist the result.

    Returns ``candidate`` unchanged, with no GitHub calls, when it already has a ``head_sha`` and no
    ``files`` to write.

    Raises:
        DisallowedCandidatePathError: a path in ``files`` or ``changed_files`` is outside the allowlist.
        UnpublishableCandidateError: there is nothing to commit, or repo, branch or base sha is empty.
    """
    disallowed = tuple(
        sorted(p for p in {*candidate.files, *candidate.changed_files} if not is_allowed_candidate_path(p))
    )
    if disallowed:
        raise DisallowedCandidatePathError(candidate.id, disallowed)
    if candidate.head_sha and not candidate.files:
        return candidate
    _require_publishable(candidate, resolved_base_sha)

    client.reset_branch(candidate.repo, candidate.branch, resolved_base_sha)
    head_sha = client.commit_files(candidate.repo, candidate.branch, candidate.files, commit_message(candidate))
    published = replace(candidate, base_sha=resolved_base_sha, head_sha=head_sha)
    ledger.add(published)
    return published


def _require_publishable(candidate: Candidate, resolved_base_sha: str) -> None:
    for what, value in (
        ("files", candidate.files),
        ("repo", candidate.repo),
        ("branch", candidate.branch),
        ("resolved_base_sha", resolved_base_sha),
    ):
        if not value:
            raise UnpublishableCandidateError.missing(candidate.id, what)


def commit_message(candidate: Candidate) -> str:
    """The Pit Crew commit message for ``candidate``: its summary, then trailers linking it to the ledger."""
    subject = candidate.summary.strip().splitlines()[0] if candidate.summary.strip() else "CI speedup candidate"
    files = "\n".join(f"- {path}" for path in sorted(candidate.files))
    return (
        f"pit-crew: {subject}\n\n"
        f"Files changed:\n{files}\n\n"
        f"Pit-Crew-Candidate: {candidate.id}\n"
        f"Pit-Crew-Hypothesis: {candidate.hypothesis_id}\n"
    )
