"""Find the commits that turned CI red, and the check that caught each one.

Input is GitHub Actions job results flattened to one row per job attempt (see `JobRun`).
A red commit is a real, replayable bug only if one of its jobs failed on every attempt.
A job that failed and then passed on a retry is flaky: replaying it says nothing about the
candidate, so it is reported separately instead.
"""

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypedDict

# A cancelled, timed-out or skipped job says nothing about the code, so only "failure" counts.
_RED = "failure"
_GREEN = "success"
_MATRIX_SUFFIX = re.compile(r"\s*\(.*\)$")

_Outcomes = dict[tuple[str, str], set[str]]
_FirstSeen = dict[str, tuple[str, str]]


class JobRun(TypedDict):
    """One attempt of one job, e.g. job "test (2/4)" of workflow "CI", attempt 1, "failure"."""

    sha: str
    branch: str
    created_at: str  # ISO 8601, so string order is time order
    workflow: str
    job: str
    attempt: int
    conclusion: str


@dataclass(frozen=True)
class RedCommit:
    """A commit with a check that failed deterministically: a bug worth replaying."""

    sha: str
    branch: str
    created_at: str
    checks: tuple[str, ...]  # "workflow/job" keys that failed on every attempt
    fixed: bool  # a later commit on the same branch passed the same check


def check_key(workflow: str, job: str) -> str:
    """Return the name a check is matched by: matrix suffixes like "test (2/4)" fold into "test"."""
    return f"{workflow}/{_fold(job)}"


def red_commits(job_runs: Iterable[JobRun]) -> list[RedCommit]:
    """Return commits with a check that failed deterministically, newest first.

    A check is red when any one of its matrix jobs failed on every attempt.
    """
    runs = list(job_runs)
    outcomes = _outcomes(runs)
    first_seen = _first_seen(runs)
    red: dict[str, set[str]] = defaultdict(set)
    for (sha, job), conclusions in outcomes.items():
        if conclusions == {_RED}:
            red[sha].add(_fold(job))

    commits = []
    for sha, checks in red.items():
        branch, created_at = first_seen[sha]
        fixed = all(_passed_later(outcomes, first_seen, sha, check) for check in checks)
        commits.append(RedCommit(sha, branch, created_at, tuple(sorted(checks)), fixed=fixed))
    return sorted(commits, key=lambda c: c.created_at, reverse=True)


def flaky_checks(job_runs: Iterable[JobRun]) -> list[tuple[str, str]]:
    """Return (sha, check) pairs where one job both failed and passed on the same commit."""
    return sorted(
        {(sha, _fold(job)) for (sha, job), conclusions in _outcomes(job_runs).items() if {_RED, _GREEN} <= conclusions}
    )


def _outcomes(job_runs: Iterable[JobRun]) -> _Outcomes:
    """Conclusions per (sha, exact job), matrix suffix kept: shard 1 failing and shard 2 passing is not a flake."""
    outcomes: _Outcomes = defaultdict(set)
    for run in job_runs:
        outcomes[(run["sha"], f"{run['workflow']}/{run['job']}")].add(run["conclusion"])
    return outcomes


def _first_seen(job_runs: Iterable[JobRun]) -> _FirstSeen:
    seen: _FirstSeen = {}
    for run in sorted(job_runs, key=lambda r: r["created_at"]):
        seen.setdefault(run["sha"], (run["branch"], run["created_at"]))
    return seen


def _passed_later(outcomes: _Outcomes, first_seen: _FirstSeen, sha: str, check: str) -> bool:
    """A later commit on the same branch ran this check and none of its jobs failed."""
    branch, created_at = first_seen[sha]
    later: dict[str, set[str]] = defaultdict(set)
    for (other, job), conclusions in outcomes.items():
        if _fold(job) == check and first_seen[other][0] == branch and first_seen[other][1] > created_at:
            later[other] |= conclusions
    return any(conclusions == {_GREEN} for conclusions in later.values())


def _fold(job: str) -> str:
    return _MATRIX_SUFFIX.sub("", job)
