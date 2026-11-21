"""Proof lane: choose the evidence to gather. The rater, not this lane, decides the verdict.

The deterministic part runs in code the dispatcher calls before starting the agent:
`touched_checks` maps the candidate's diff to checks, `red_commits` mines CI history, and
`pick_replays` chooses the bugs those checks caught. The agent then fills the gaps
`uncovered_checks` reports, using `propose_mutants`, and records everything with `plan_replays`.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict

from pit_crew.contracts import BugCase, Candidate, ReplayPlan
from pit_crew.lanes.proof.checks import Check
from pit_crew.lanes.proof.miner import RedCommit
from pit_crew.lanes.proof.mutants import mutants
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

MAX_REPLAYS = 20
_SHA = re.compile(r"^[0-9a-f]{4,40}$")  # git's shortest abbreviation up to a full SHA-1


class InvalidReplayPlanError(ValueError):
    """Raised when plan_replays is given nothing to replay, or something that is not a bug."""

    @classmethod
    def empty(cls) -> "InvalidReplayPlanError":
        """Build the error for a plan with no bugs in it."""
        return cls("a replay plan needs at least one bug; the rater rejects a candidate with none")

    @classmethod
    def not_shas(cls, bad: Iterable[str]) -> "InvalidReplayPlanError":
        """Build the error for values that are not commit SHAs."""
        return cls(f"not commit SHAs: {', '.join(bad)}")

    @classmethod
    def bad_mutation(cls, path: str) -> "InvalidReplayPlanError":
        """Build the error for a mutation without a relative path and new contents."""
        return cls(f"a mutation needs a relative path and the whole mutated file, got path {path!r}")

    @classmethod
    def disallowed_mutation_path(cls, path: str) -> "InvalidReplayPlanError":
        """Build the error for a mutation targeting workflow or GitHub configuration files."""
        return cls(f"mutations may not modify .github files, got path {path!r}")


def pick_replays(red: Iterable[RedCommit], touched: Iterable[Check] | None, limit: int = MAX_REPLAYS) -> list[str]:
    """Return bugs the touched checks caught, taking turns between checks so each gets coverage.

    Within a check, bugs that were later fixed come first (a red-to-green fix is a real
    bug, not a broken branch), then the newest. touched=None means every check.
    """
    keys = None if touched is None else {key for check in touched for key in check}
    queues: dict[str, list[str]] = defaultdict(list)
    for commit in sorted(red, key=lambda c: not c.fixed):  # stable: keeps newest first
        for check in commit.checks:
            if keys is None or check in keys:
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


def uncovered_checks(red: Iterable[RedCommit], touched: Iterable[Check]) -> set[Check]:
    """Return touched checks with no bug recorded under any of their keys.

    Each needs a mutant, or the change is unproven there.
    """
    caught = {key for commit in red for key in commit.checks}
    return {check for check in touched if check.isdisjoint(caught)}


@tool(
    lane="proof",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repository path of the source file"},
            "source": {"type": "string", "description": "The file's full contents"},
            "limit": {"type": "integer", "minimum": 1, "description": "Most mutants to return (default 50)"},
        },
        "required": ["path", "source"],
    },
)
def propose_mutants(
    ledger: Ledger,  # noqa: ARG001  every tool takes the ledger first
    path: str,
    source: str,
    limit: int = 50,
) -> list[dict[str, object]]:
    """One-operator mutants of a source file, each a one-line bug: {path, line, before, after, operator}.

    Replace line `line` (1-based) of the file with `after` and pass the whole mutated file
    to plan_replays as a mutation.
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
                "description": "Commits that turned CI red and must still fail on the candidate",
            },
            "mutations": {
                "type": "array",
                "description": "Synthetic bugs for checks with no recorded failure",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Repository path of the mutated file"},
                        "content": {"type": "string", "description": "The whole file, with the bug in it"},
                        "reason": {"type": "string", "description": "Which check should catch it, and why"},
                    },
                    "required": ["path", "content", "reason"],
                },
            },
        },
        "required": ["candidate_id"],
    },
)
def plan_replays(
    ledger: Ledger,
    candidate_id: str,
    bug_shas: list[str] | None = None,
    mutations: list[dict[str, str]] | None = None,
) -> str:
    """Choose which bugs to replay against a candidate: red commits and mutated files. Returns the plan id."""
    ledger.get(Candidate, candidate_id)  # fail on an unknown candidate
    shas = tuple(dict.fromkeys(sha.strip().lower() for sha in bug_shas or ()))
    if bad := [sha for sha in shas if not _SHA.match(sha)]:
        raise InvalidReplayPlanError.not_shas(bad)
    cases = [BugCase(kind="commit", commit_sha=sha, reason="historical red commit") for sha in shas]
    cases += [_mutation_case(m) for m in mutations or ()]
    if not cases:
        raise InvalidReplayPlanError.empty()
    plan = ReplayPlan(candidate_id, bug_shas=shas, cases=tuple(cases))
    ledger.add(plan)
    return plan.id


def _mutation_case(mutation: Mapping[str, str]) -> BugCase:
    path, content = mutation.get("path", ""), mutation.get("content", "")
    parts = path.split("/")
    # Every segment must name a file or folder: no "", ".", "..", or Windows separators.
    if not content or "\\" in path or any(part in {"", ".", ".."} for part in parts):
        raise InvalidReplayPlanError.bad_mutation(path)
    if parts[0].lower() == ".github":
        raise InvalidReplayPlanError.disallowed_mutation_path(path)
    return BugCase(
        kind="mutation", mutated_files={path: content}, reason=mutation.get("reason") or "synthetic mutation"
    )
