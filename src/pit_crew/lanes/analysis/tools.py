"""Analysis lane: find where the minutes go and record hypotheses."""

from collections import defaultdict
from collections.abc import Sequence
from typing import TypedDict

from pit_crew.contracts import Hypothesis
from pit_crew.ledger import Ledger
from pit_crew.tools import tool


class JobRun(TypedDict):
    """Timing entry for a single CI job execution."""

    name: str
    seconds: float


def slowest_jobs(job_runs: Sequence[JobRun]) -> list[tuple[str, float]]:
    """Total seconds per job across runs, slowest first.

    Each item needs "name" and "seconds".
    """
    totals: dict[str, float] = defaultdict(float)
    for run in job_runs:
        totals[run["name"]] += run["seconds"]
    return sorted(totals.items(), key=lambda item: item[1], reverse=True)


@tool(
    lane="analysis",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name of the fork"},
            "description": {"type": "string", "description": "The bottleneck, with the evidence for it"},
            "skill": {"type": "string", "description": "Playbook skill that should fix it"},
            "expected_savings_seconds": {"type": "number"},
        },
        "required": ["repo", "description", "skill", "expected_savings_seconds"],
    },
)
def record_hypothesis(ledger: Ledger, repo: str, description: str, skill: str, expected_savings_seconds: float) -> str:
    """Record one bottleneck worth fixing. Returns the hypothesis id."""
    hypothesis = Hypothesis(repo, description, skill, expected_savings_seconds)
    ledger.add(hypothesis)
    return hypothesis.id
