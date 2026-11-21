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

from pit_crew.contracts import BugCase, Candidate, ReplayPlan, is_allowed_candidate_path
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
    def not_a_list(cls, field: str) -> "InvalidReplayPlanError":
        """Build the error for bug_shas or mutations given as something other than a list."""
        return cls(f"{field} must be a list")

    @classmethod
    def bad_mutation(cls, path: object) -> "InvalidReplayPlanError":
        """Build the error for a mutation without a relative path and new contents."""
        return cls(f"a mutation needs a relative path and the whole mutated file, got path {path!r}")

    @classmethod
    def mutates_the_candidate(cls, path: str) -> "InvalidReplayPlanError":
        """Build the error for a mutation of CI config or of a file the candidate changed."""
        return cls(
            f"a mutation must change code under test, not {path!r}: the candidate may change that file, "
            "so replaying the mutation would overwrite the change it is meant to test"
        )


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
    candidate = ledger.get(Candidate, candidate_id)  # fail on an unknown candidate
    shas = _shas(_listed(bug_shas, "bug_shas"))
    cases = [BugCase(kind="commit", commit_sha=sha, reason="historical red commit") for sha in shas]
    cases += [_mutation_case(m, candidate) for m in _listed(mutations, "mutations")]
    if not cases:
        raise InvalidReplayPlanError.empty()
    plan = ReplayPlan(candidate_id, bug_shas=shas, cases=tuple(cases))
    ledger.add(plan)
    return plan.id


# The model writes these arguments, so each has its type checked here: a wrong one must come
# back to the model as an error it can fix, not crash the lane.


def _listed(value: object, field: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidReplayPlanError.not_a_list(field)
    return value


def _shas(values: list[object]) -> tuple[str, ...]:
    # A number is refused, not read as hex: 0012345 written as a number is the wrong commit.
    shas = [v.strip().lower() for v in values if isinstance(v, str)]
    if bad := [repr(v) for v in values if not isinstance(v, str)] + [sha for sha in shas if not _SHA.match(sha)]:
        raise InvalidReplayPlanError.not_shas(bad)
    return tuple(dict.fromkeys(shas))


def _mutation_case(mutation: object, candidate: Candidate) -> BugCase:
    fields = mutation if isinstance(mutation, Mapping) else {}
    path, content, reason = (fields.get(k, "") for k in ("path", "content", "reason"))
    if not (isinstance(path, str) and isinstance(content, str) and isinstance(reason, str)) or not content:
        raise InvalidReplayPlanError.bad_mutation(path)
    # Every segment must name a file or folder: no "", ".", "..", or Windows separators.
    if "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise InvalidReplayPlanError.bad_mutation(path)
    if is_allowed_candidate_path(path) or path in candidate.changed_files:
        raise InvalidReplayPlanError.mutates_the_candidate(path)
    return BugCase(kind="mutation", mutated_files={path: content}, reason=reason or "synthetic mutation")
