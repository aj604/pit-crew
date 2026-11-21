"""Proof lane: choose the evidence to gather. The rater, not this lane, decides the verdict.

The deterministic part runs in code the dispatcher calls before starting the agent:
`touched_checks` maps the candidate's diff to checks, `red_commits` mines CI history, and
`pick_replays` chooses the bugs those checks caught. The agent then fills the gaps
`uncovered_checks` reports, using `propose_mutants`, and records everything with `plan_replays`.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict

from pit_crew.contracts import BugCase, Candidate, ReplayPlan
from pit_crew.lanes.proof.checks import Check
from pit_crew.lanes.proof.miner import RedCommit
from pit_crew.lanes.proof.mutants import mutants
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

MAX_REPLAYS = 20
_SHA = re.compile(r"^[0-9a-f]{4,40}$")  # git's shortest abbreviation up to a full SHA-1
_DEFAULT_COMMIT_REASON = "historical red commit"


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
    def bad_expected_checks(cls, value: object) -> "InvalidReplayPlanError":
        """Build the error for expected_checks that is not a non-empty list of check names."""
        return cls(f"expected_checks must be a non-empty list of check names, got {value!r}")


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


def expected_checks_for(commit: RedCommit, touched: Iterable[Check] | None) -> tuple[str, ...]:
    """Return the check keys expected to fail when replaying ``commit`` against ``touched``.

    When ``touched`` is given, each touched check that caught ``commit`` contributes all of its
    aliases (job id, display name, and old/new names across renames) so a renamed job still
    matches. Falls back to ``commit.checks`` when ``touched`` is None or does not name the
    commit's checks.
    """
    if touched is not None:
        matched = {key for check in touched if not check.isdisjoint(commit.checks) for key in check}
        if matched:
            return tuple(sorted(matched))
    return tuple(sorted(set(commit.checks)))


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
                "description": "Commits that turned CI red without structured expected_checks; prefer `commits`",
            },
            "commits": {
                "type": "array",
                "description": "Historical red commits with the CI check(s) expected to fail on the candidate",
                "items": {
                    "type": "object",
                    "properties": {
                        "sha": {"type": "string", "description": "Commit SHA that turned CI red"},
                        "expected_checks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "CI check keys (workflow/job, including id and display-name aliases) "
                                "expected to fail on this commit"
                            ),
                        },
                        "reason": {"type": "string", "description": "Why this commit is replayed"},
                    },
                    "required": ["sha", "expected_checks"],
                },
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
                        "expected_checks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "CI check keys (workflow/job, including id and display-name aliases) "
                                "expected to catch this mutation"
                            ),
                        },
                    },
                    "required": ["path", "content", "reason", "expected_checks"],
                },
            },
        },
        "required": ["candidate_id"],
    },
)
def plan_replays(
    ledger: Ledger,
    candidate_id: str,
    bug_shas: Sequence[str] | None = None,
    mutations: Sequence[Mapping[str, object]] | None = None,
    commits: Sequence[Mapping[str, object]] | None = None,
) -> str:
    """Choose which bugs to replay against a candidate: red commits and mutated files. Returns the plan id."""
    ledger.get(Candidate, candidate_id)  # fail on an unknown candidate
    shas, cases = _commit_cases(bug_shas, commits)
    cases += [_mutation_case(m) for m in mutations or ()]
    if not cases:
        raise InvalidReplayPlanError.empty()
    plan = ReplayPlan(candidate_id, bug_shas=shas, cases=tuple(cases))
    ledger.add(plan)
    return plan.id


def _commit_cases(
    bug_shas: Sequence[str] | None,
    commits: Sequence[Mapping[str, object]] | None,
) -> tuple[tuple[str, ...], list[BugCase]]:
    raw_entries: list[tuple[object, tuple[str, ...], str]] = [(sha, (), "") for sha in bug_shas or ()]
    for entry in commits or ():
        raw_reason = entry.get("reason")
        reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
        raw_entries.append((entry.get("sha"), _parse_expected_checks(entry.get("expected_checks")), reason))

    bad: list[str] = []
    merged_checks: dict[str, list[str]] = {}
    merged_reason: dict[str, str] = {}
    for raw_sha, checks, reason in raw_entries:
        sha = raw_sha.strip().lower() if isinstance(raw_sha, str) else ""
        if not _SHA.match(sha):
            bad.append(sha if isinstance(raw_sha, str) else repr(raw_sha))
            continue
        bucket = merged_checks.setdefault(sha, [])
        for check in checks:
            if check not in bucket:
                bucket.append(check)
        if reason and not merged_reason.get(sha):
            merged_reason[sha] = reason

    if bad:
        raise InvalidReplayPlanError.not_shas(dict.fromkeys(bad))

    shas = tuple(merged_checks)
    cases = [
        BugCase(
            kind="commit",
            commit_sha=sha,
            reason=merged_reason.get(sha) or _DEFAULT_COMMIT_REASON,
            expected_checks=tuple(checks),
        )
        for sha, checks in merged_checks.items()
    ]
    return shas, cases


def _mutation_case(mutation: Mapping[str, object]) -> BugCase:
    raw_path, raw_content = mutation.get("path", ""), mutation.get("content", "")
    path = raw_path if isinstance(raw_path, str) else ""
    content = raw_content if isinstance(raw_content, str) else ""
    # Every segment must name a file or folder: no "", ".", "..", or Windows separators.
    if not content or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise InvalidReplayPlanError.bad_mutation(path)
    raw_reason = mutation.get("reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) and raw_reason.strip() else "synthetic mutation"
    expected_checks = _parse_expected_checks(mutation["expected_checks"]) if "expected_checks" in mutation else ()
    return BugCase(
        kind="mutation",
        mutated_files={path: content},
        reason=reason,
        expected_checks=expected_checks,
    )


def _parse_expected_checks(raw: object) -> tuple[str, ...]:
    if isinstance(raw, str | bytes) or not isinstance(raw, Sequence) or not raw:
        raise InvalidReplayPlanError.bad_expected_checks(raw)
    checks: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise InvalidReplayPlanError.bad_expected_checks(raw)
        stripped = item.strip()
        if stripped not in checks:
            checks.append(stripped)
    return tuple(checks)
