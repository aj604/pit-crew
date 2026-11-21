"""Which bottlenecks are already recorded, so the Analysis agent never records one twice.

A bottleneck is identified by the playbook skill that fixes it and where the fix goes
(workflow file, job, step). Open, rejected and kept hypotheses all block a repeat: open ones
are in flight, rejected ones failed, and kept ones are already fixed.
"""

from pit_crew.ledger import Ledger

type HypothesisKey = tuple[str, str, str, str]


class DuplicateHypothesisError(ValueError):
    """Raised when a bottleneck is already in the ledger."""

    @classmethod
    def already_recorded(cls, skill: str, workflow_path: str, job: str, step: str) -> "DuplicateHypothesisError":
        """Build error for a key that is already recorded."""
        return cls(
            f"already recorded: skill={skill!r} workflow_path={workflow_path!r} job={job!r} step={step!r}. "
            "Record a different bottleneck."
        )


def hypothesis_key(skill: str, workflow_path: str, job: str, step: str) -> HypothesisKey:
    """The identity of a bottleneck: the same skill aimed at the same place."""
    return (skill, workflow_path, job, step)


def recorded_keys(ledger: Ledger, repo: str) -> set[HypothesisKey]:
    """Keys of every hypothesis already recorded for ``repo``, whatever happened to it since."""
    return {hypothesis_key(h.skill, h.workflow_path, h.job, h.step) for h in ledger.hypotheses_for_repo(repo)}
