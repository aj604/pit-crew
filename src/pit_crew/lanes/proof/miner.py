"""Find the commits that turned CI red, and the check that caught each one.

Input is GitHub Actions job results flattened to one row per job attempt:

    {"sha": "b2c3d4e", "branch": "feat", "created_at": "2026-11-02T10:00:00Z",
     "workflow": "CI", "job": "test (2/4)", "attempt": 1, "conclusion": "failure"}

A red commit is a real, replayable bug only if its check failed on every attempt. A check
that failed and then passed on a retry is flaky: replaying it says nothing about the
candidate, so it is reported separately instead.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

# A cancelled, timed-out or skipped job says nothing about the code, so only "failure" counts.
_RED = "failure"
_GREEN = "success"
_MATRIX_SUFFIX = re.compile(r"\s*\(.*\)$")


@dataclass(frozen=True)
class RedCommit:
    sha: str
    branch: str
    created_at: str
    checks: tuple[str, ...]  # "workflow/job" keys that failed on every attempt
    fixed: bool  # a later commit on the same branch passed the same check


def check_key(workflow: str, job: str) -> str:
    """The name a check is matched by: matrix suffixes like "test (2/4)" fold into "test"."""
    return f"{workflow}/{_MATRIX_SUFFIX.sub('', job)}"


def red_commits(job_runs: list[dict]) -> list[RedCommit]:
    """Commits whose checks failed deterministically, newest first.

    A check is red when any one of its matrix jobs failed on every attempt.
    """
    outcomes = _outcomes(job_runs)
    first_seen = _first_seen(job_runs)
    red: dict[str, set[str]] = defaultdict(set)
    for (sha, job), conclusions in outcomes.items():
        if conclusions == {_RED}:
            red[sha].add(_fold(job))

    commits = []
    for sha, checks in red.items():
        branch, created_at = first_seen[sha]
        fixed = all(_passed_later(outcomes, first_seen, sha, check) for check in checks)
        commits.append(RedCommit(sha, branch, created_at, tuple(sorted(checks)), fixed))
    return sorted(commits, key=lambda c: c.created_at, reverse=True)


def flaky_checks(job_runs: list[dict]) -> list[tuple[str, str]]:
    """(sha, check) pairs where one job both failed and passed on the same commit. Each is a bug to fix."""
    return sorted(
        {(sha, _fold(job)) for (sha, job), conclusions in _outcomes(job_runs).items() if {_RED, _GREEN} <= conclusions}
    )


def _outcomes(job_runs: list[dict]) -> dict[tuple[str, str], set[str]]:
    """Conclusions per (sha, exact job), matrix suffix kept: shard 1 failing and shard 2 passing is not a flake."""
    outcomes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in job_runs:
        outcomes[(run["sha"], f"{run['workflow']}/{run['job']}")].add(run["conclusion"])
    return outcomes


def _fold(job: str) -> str:
    return _MATRIX_SUFFIX.sub("", job)


def _first_seen(job_runs: list[dict]) -> dict[str, tuple[str, str]]:
    seen: dict[str, tuple[str, str]] = {}
    for run in sorted(job_runs, key=lambda r: r["created_at"]):
        seen.setdefault(run["sha"], (run["branch"], run["created_at"]))
    return seen


def _passed_later(
    outcomes: dict[tuple[str, str], set[str]], first_seen: dict[str, tuple[str, str]], sha: str, check: str
) -> bool:
    """A later commit on the same branch ran this check and none of its jobs failed."""
    branch, created_at = first_seen[sha]
    later: dict[str, set[str]] = defaultdict(set)
    for (other, job), conclusions in outcomes.items():
        if _fold(job) == check and first_seen[other][0] == branch and first_seen[other][1] > created_at:
            later[other] |= conclusions
    return any(conclusions == {_GREEN} for conclusions in later.values())
