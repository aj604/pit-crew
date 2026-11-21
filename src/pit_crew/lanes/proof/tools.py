"""Proof lane: choose the evidence to gather. The rater, not this lane, decides the verdict.

The deterministic part runs in code the dispatcher calls before starting the agent:
`touched_checks` maps the candidate's diff to checks, `red_commits` mines CI history, and
`pick_replays` chooses the bugs those checks caught. The agent then fills the gaps
`uncovered_checks` reports, using `propose_mutants`, and records everything with `plan_replays`.
"""

import re
from collections import defaultdict
from dataclasses import asdict

from pit_crew.contracts import Candidate, ReplayPlan
from pit_crew.lanes.proof.miner import RedCommit
from pit_crew.lanes.proof.mutants import mutants
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

MAX_REPLAYS = 20
_SHA = re.compile(r"^[0-9a-f]{4,40}$")  # git's shortest abbreviation up to a full SHA-1


def pick_replays(red: list[RedCommit], touched: set[str] | None, limit: int = MAX_REPLAYS) -> list[str]:
    """Bugs the touched checks caught, taking turns between checks so each gets coverage.

    Within a check, bugs that were later fixed come first (a red-to-green fix is a real
    bug, not a broken branch), then the newest. touched=None means every check.
    """
    queues: dict[str, list[str]] = defaultdict(list)
    for commit in sorted(red, key=lambda c: not c.fixed):  # stable: keeps newest first
        for check in commit.checks:
            if touched is None or check in touched:
                queues[check].append(commit.sha)

    picked: list[str] = []
    while len(picked) < limit and any(queues.values()):
        for check in sorted(queues):
            queue = queues[check]
            while queue and queue[0] in picked:
                queue.pop(0)
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked


def uncovered_checks(red: list[RedCommit], touched: set[str]) -> set[str]:
    """Touched checks with no recorded bug. Each needs a mutant, or the change is unproven there."""
    caught = {check for commit in red for check in commit.checks}
    return touched - caught


@tool(
    lane="proof",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository path of the source file"},
            "source": {"type": "string", "description": "The file's full contents"},
            "limit": {"type": "integer", "description": "Most mutants to return (default 50)"},
        },
        "required": ["path", "source"],
    },
)
def propose_mutants(ledger: Ledger, path: str, source: str, limit: int = 50) -> list[dict]:
    """One-operator mutants of a source file, each a one-line bug: {path, line, before, after, operator}.

    Replace line `line` (1-based) of `path` with `after`, commit that one change on a new
    branch off the candidate's base, push it, and add the commit to the replay plan.
    """
    return [asdict(m) for m in mutants(path, source, limit)]


@tool(
    lane="proof",
    parameters={
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string"},
            "bug_shas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Commits that turned CI red, or pushed mutant commits, that must still fail on the candidate",
            },
        },
        "required": ["candidate_id", "bug_shas"],
    },
)
def plan_replays(ledger: Ledger, candidate_id: str, bug_shas: list[str]) -> str:
    """Choose which bugs to replay against a candidate. Returns the plan id."""
    ledger.get(Candidate, candidate_id)  # fail on an unknown candidate
    shas = tuple(dict.fromkeys(sha.strip().lower() for sha in bug_shas))
    if not shas:
        raise ValueError("a replay plan needs at least one bug; the rater rejects a candidate with none")
    if bad := [sha for sha in shas if not _SHA.match(sha)]:
        raise ValueError(f"not commit SHAs: {', '.join(bad)}")
    plan = ReplayPlan(candidate_id, shas)
    ledger.add(plan)
    return plan.id
