"""Readiness preflight for a benchmark target: can Analysis and Proof both work against it?

``pit-crew check-target --repo <owner>/<repo>`` runs before Milestone 2 (``smoke-lane``) and
Milestone 3 (``run-cycle --until-sla``). It reads the target's Actions history once and checks:

* Analysis: ``find_bottlenecks`` ranks at least ``MIN_FINDINGS`` findings in ``PLAYBOOK_SKILLS``
  that each save more than 0 seconds.
* Proof: every required check (``REQUIRED_CHECKS``) caught at least one deterministic bug that a
  later commit fixed (a ``RedCommit`` with ``fixed=True``), so ``uncovered_checks`` is empty.

This module sits above the lanes so it may import both; the lanes never import each other.
"""

import importlib
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pit_crew.benchmark_seeder import DEFAULT_TEMPLATE, DISPATCH_EVENT, MAIN, WORKFLOW_FILE, WORKFLOW_PATH
from pit_crew.contracts import PLAYBOOK_SKILLS
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.stats import percentile
from pit_crew.lanes.proof import checks, miner
from pit_crew.lanes.proof.tools import uncovered_checks
from pit_crew.ledger import InMemoryLedger

MIN_FINDINGS = 3
REQUIRED_CHECKS: tuple[str, ...] = ("lint", "typecheck", "unit-test", "ui-check")  # job ids in the frozen ci.yml
WORKSPACE_ENV = "PIT_CREW_WORKSPACE"
DEFAULT_RUNS = history.DEFAULT_RUNS
Getter = history.Getter
_FIND_BOTTLENECKS_MODULES = ("pit_crew.lanes.analysis", "pit_crew.lanes.analysis.tools")

FindBottlenecks = Callable[[str, str], Mapping[str, Any]]
"""``(repo, workflow file) -> find_bottlenecks output``: a mapping whose ``findings`` is a list of dicts."""


class FindBottlenecksUnavailableError(NotImplementedError):
    """Raised when the Analysis lane's ``find_bottlenecks`` is not installed in this checkout."""

    def __init__(self) -> None:
        """Say where it was looked for."""
        super().__init__(
            "Analysis find_bottlenecks is not yet installed (looked in "
            f"{', '.join(_FIND_BOTTLENECKS_MODULES)}); land the Analysis detectors first"
        )


@contextmanager
def _workspace(path: Path) -> Iterator[None]:
    previous = os.environ.get(WORKSPACE_ENV)
    os.environ[WORKSPACE_ENV] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(WORKSPACE_ENV, None)
        else:
            os.environ[WORKSPACE_ENV] = previous


def default_find_bottlenecks(workspace: Path = DEFAULT_TEMPLATE) -> FindBottlenecks:
    """The Analysis lane's ``find_bottlenecks`` tool, adapted to ``(repo, workflow)``.

    It reads the workflow file from ``workspace`` (default: the template whose ``ci.yml`` the
    seeder keeps frozen on the target) and dedupes against a fresh, empty ledger, so findings
    already recorded elsewhere still count. Raises ``FindBottlenecksUnavailableError`` when the
    tool has not landed yet.
    """
    for name in _FIND_BOTTLENECKS_MODULES:
        found = getattr(importlib.import_module(name), "find_bottlenecks", None)
        if callable(found):
            tool: Callable[..., Mapping[str, Any]] = found
            break
    else:
        raise FindBottlenecksUnavailableError

    def run(repo: str, workflow: str) -> Mapping[str, Any]:
        with _workspace(workspace):
            return tool(InMemoryLedger(), repo, workflow)

    return run


@dataclass(frozen=True)
class Readiness:
    """The preflight's verdict, printed by the CLI as JSON."""

    repo: str
    base_sha: str | None
    green_runs: int
    baseline_p50_seconds: float
    baseline_p95_seconds: float
    findings_count: int
    red_commits_by_check: dict[str, list[str]]
    problems: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True only when no preflight assertion failed."""
        return not self.problems

    def as_json(self) -> dict[str, Any]:
        """A JSON-ready report."""
        return {
            "repo": self.repo,
            "base_sha": self.base_sha,
            "green_runs": self.green_runs,
            "baseline_p50_seconds": self.baseline_p50_seconds,
            "baseline_p95_seconds": self.baseline_p95_seconds,
            "findings_count": self.findings_count,
            "red_commits_by_check": self.red_commits_by_check,
            "ready": self.ready,
            "problems": self.problems,
        }


def to_proof_job_runs(raw_runs: Sequence[Mapping[str, Any]]) -> list[miner.JobRun]:
    """Flatten Analysis ``fetch_history`` output into Proof's one-row-per-job-attempt ``JobRun`` rows.

    Proof keys a check as ``"<workflow name>/<job display name>"``, so the run's workflow ``name``
    and each job's display ``name`` are kept as GitHub reports them.
    """
    return [
        miner.JobRun(
            sha=str(run.get("head_sha") or ""),
            branch=str(run.get("head_branch") or ""),
            created_at=str(job.get("created_at") or run.get("created_at") or ""),
            workflow=str(run.get("name") or ""),
            job=str(job.get("name") or ""),
            attempt=int(job.get("run_attempt") or run.get("run_attempt") or 1),
            conclusion=str(job.get("conclusion") or ""),
        )
        for run in raw_runs
        for job in run.get("jobs") or ()
    ]


def _baseline(raw_runs: Sequence[Mapping[str, Any]]) -> tuple[str | None, list[float]]:
    """Newest green baseline run's head SHA, and every green baseline run's wall-clock seconds.

    A baseline run is a successful ``workflow_dispatch`` run on main of the frozen workflow file,
    which is what ``seed-target`` produces. ``fetch_history`` returns runs newest first.
    """
    green = [
        r
        for r in raw_runs
        if r.get("event") == DISPATCH_EVENT
        and r.get("head_branch") == MAIN
        and r.get("conclusion") == "success"
        and str(r.get("path") or WORKFLOW_PATH).split("@", 1)[0] == WORKFLOW_PATH
    ]
    walls = [run.wall_clock_seconds for run in history.parse_runs([dict(r) for r in green])]
    return (str(green[0].get("head_sha")) if green else None), walls


def _required_checks(workflow_text: str) -> tuple[dict[str, checks.Check], list[str]]:
    """Each required job's check keys (by job id and display name), and problems finding them."""
    workflow = checks.parse_workflow(workflow_text, WORKFLOW_PATH)
    if workflow is None:
        return {}, [f"{WORKFLOW_PATH} is not a parseable workflow"]
    found = {job_id: workflow.keys(job_id) for job_id in REQUIRED_CHECKS}
    missing = [job_id for job_id, keys in found.items() if not keys]
    return {k: v for k, v in found.items() if v}, [
        f"required check {job_id!r} is not a job in {WORKFLOW_PATH}" for job_id in missing
    ]


def _qualifying(findings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    out = []
    for finding in findings:
        try:
            savings = float(finding.get("expected_savings_seconds") or 0)
        except (TypeError, ValueError):
            continue
        if finding.get("skill") in PLAYBOOK_SKILLS and savings > 0:
            out.append(finding)
    return out


def check_target(  # noqa: PLR0913  keyword-only seams for offline tests
    repo: str,
    *,
    workflow: str = WORKFLOW_FILE,
    runs: int = DEFAULT_RUNS,
    get: Getter | None = None,
    find_bottlenecks: FindBottlenecks | None = None,
    workflow_text: str | None = None,
) -> Readiness:
    """Run the Analysis and Proof preflight against ``repo`` and return its readiness.

    ``get`` replaces the GitHub API getter ``fetch_history`` uses; ``find_bottlenecks`` replaces the
    Analysis tool (default: ``default_find_bottlenecks()``); ``workflow_text`` replaces the frozen
    ``ci.yml`` from the benchmark template, which names the required checks.
    """
    raw = history.fetch_history(repo, workflow, runs=runs, get=get)
    base_sha, walls = _baseline(raw)
    problems: list[str] = []
    if not walls:
        problems.append(f"no green {DISPATCH_EVENT} baseline runs of {WORKFLOW_PATH} on {MAIN}")

    finder = find_bottlenecks if find_bottlenecks is not None else default_find_bottlenecks()
    findings = _qualifying(finder(repo, workflow).get("findings") or [])
    if len(findings) < MIN_FINDINGS:
        problems.append(
            f"Analysis found {len(findings)} ranked PLAYBOOK_SKILLS finding(s) saving > 0s; "
            f"need at least {MIN_FINDINGS}"
        )

    text = workflow_text if workflow_text is not None else (DEFAULT_TEMPLATE / WORKFLOW_PATH).read_text()
    required, missing = _required_checks(text)
    problems.extend(missing)
    fixed = [c for c in miner.red_commits(to_proof_job_runs(raw)) if c.fixed]
    by_check = {job_id: [c.sha for c in fixed if keys.intersection(c.checks)] for job_id, keys in required.items()}
    uncovered = uncovered_checks(fixed, required.values())
    problems.extend(
        f"Proof has no fixed deterministic red commit for required check {job_id!r}"
        for job_id, keys in required.items()
        if keys in uncovered
    )

    return Readiness(
        repo=repo,
        base_sha=base_sha,
        green_runs=len(walls),
        baseline_p50_seconds=percentile(walls, 50),
        baseline_p95_seconds=percentile(walls, 95),
        findings_count=len(findings),
        red_commits_by_check=by_check,
        problems=problems,
    )
