"""Proof lane: choose the evidence to gather. The rater, not this lane, decides the verdict."""

from pit_crew.contracts import Candidate, ReplayPlan
from pit_crew.ledger import Ledger
from pit_crew.tools import tool


@tool(
    lane="proof",
    parameters={
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "bug_shas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Commits that turned CI red and must still fail on the candidate",
            },
        },
        "required": ["candidate_id", "bug_shas"],
    },
)
def plan_replays(ledger: Ledger, candidate_id: str, bug_shas: list[str]) -> str:
    """Choose which historical bugs to replay against a candidate. Returns the plan id."""
    ledger.get(Candidate, candidate_id)  # fail on an unknown candidate
    plan = ReplayPlan(candidate_id, tuple(bug_shas))
    ledger.add(plan)
    return plan.id
