"""Decides which lane runs next. Holds no other logic.

TODO(dispatcher owner): the service that receives GitHub webhooks, collects Evidence
from Actions runs, and calls managed_agent.run_lane.
"""

from pit_crew.contracts import Candidate, Hypothesis, Record


def next_lane(record: Record) -> str | None:
    """The lane a new ledger record starts, or None if it starts no agent work."""
    if isinstance(record, Hypothesis):
        return "optimizer"
    if isinstance(record, Candidate):
        return "proof"
    return None
