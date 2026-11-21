"""Optimizer lane: make one change on a branch and report it."""

from pit_crew.contracts import Candidate, Hypothesis
from pit_crew.ledger import Ledger
from pit_crew.tools import tool


@tool(
    lane="optimizer",
    parameters={
        "type": "object",
        "properties": {
            "hypothesis_id": {"type": "string"},
            "branch": {"type": "string", "description": "Branch pushed to the fork"},
            "summary": {"type": "string", "description": "What changed and why it should be faster"},
        },
        "required": ["hypothesis_id", "branch", "summary"],
    },
)
def report_candidate(ledger: Ledger, hypothesis_id: str, branch: str, summary: str) -> str:
    """Report the pushed branch for a hypothesis. Returns the candidate id."""
    ledger.get(Hypothesis, hypothesis_id)  # fail on an unknown hypothesis
    candidate = Candidate(hypothesis_id, branch, summary)
    ledger.add(candidate)
    return candidate.id
