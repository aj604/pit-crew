"""Analysis lane: find where the minutes go and record hypotheses.

Pure logic lives in `history.py` (fetch + parse run history), `workflow.py` (job graph) and
`critical_path.py` (profiling). This module only wraps it as agent tools.
"""

import math
import re
import urllib.error
from pathlib import Path
from typing import Any

from pit_crew import workspace as host_workspace
from pit_crew.contracts import Hypothesis
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.critical_path import CIProfile, profile
from pit_crew.lanes.analysis.dedupe_ledger import DuplicateHypothesisError, hypothesis_key, recorded_keys
from pit_crew.lanes.analysis.detectors import DETECTORS
from pit_crew.lanes.analysis.detectors.base import keep_worthwhile
from pit_crew.lanes.analysis.workflow import Workflow, parse_workflow
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW_FILE = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")
_HTTP_NOT_FOUND = 404


class WorkflowNotFoundError(FileNotFoundError):
    """Raised when a remote workflow cannot be resolved for a deleted branch."""

    @classmethod
    def no_recorded_commit(cls, repo: str, workflow: str, branch: str) -> "WorkflowNotFoundError":
        """Build error when a deleted branch has no completed runs with a commit SHA."""
        return cls(
            f"branch {branch!r} not found and no completed runs record a commit for {repo}/.github/workflows/{workflow}"
        )

    @classmethod
    def inaccessible_commit(cls, repo: str, workflow: str, branch: str, sha: str) -> "WorkflowNotFoundError":
        """Build error when neither the deleted branch nor its latest run's commit is accessible."""
        return cls(
            f"branch {branch!r} not found and run commit {sha!r} could not be fetched for "
            f"{repo}/.github/workflows/{workflow}"
        )


def workspace() -> Path:
    """Where the fork is checked out: the context's workspace, else PIT_CREW_WORKSPACE, else /workspace."""
    return host_workspace.active_workspace()


def _validate_workflow_name(workflow: str) -> None:
    if not _WORKFLOW_FILE.fullmatch(workflow) or workflow.startswith("."):
        msg = f"workflow must be a file name like ci.yml, got {workflow!r}"
        raise ValueError(msg)


def workflow_path(workflow: str) -> Path:
    """The workflow file inside the workspace. Rejects anything but a bare .yml/.yaml file name."""
    _validate_workflow_name(workflow)
    folder = (workspace() / ".github" / "workflows").resolve()
    path = (folder / workflow).resolve()
    if path.parent != folder:  # a symlink pointing out of the folder
        msg = f"workflow {workflow!r} resolves outside .github/workflows"
        raise ValueError(msg)
    return path


def _load_workflow_and_history(  # noqa: PLR0913  mirrors profile_ci parameters
    repo: str,
    workflow: str,
    *,
    runs: int,
    branch: str | None,
    workflow_content: str | None,
    ref: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Load workflow YAML and run history, falling back to a run's commit SHA if ``branch`` was deleted."""
    if workflow_content is not None:
        _validate_workflow_name(workflow)
        return workflow_content, history.fetch_history(repo, workflow, runs=runs, branch=branch)

    path = workflow_path(workflow)
    if path.is_file():
        return path.read_text(), history.fetch_history(repo, workflow, runs=runs, branch=branch)

    fetch_ref = ref or branch
    try:
        yaml_text = history.fetch_workflow(repo, workflow, ref=fetch_ref)
    except urllib.error.HTTPError as exc:
        if ref is not None or branch is None or exc.code != _HTTP_NOT_FOUND:
            raise
        raw = history.fetch_history(repo, workflow, runs=runs, branch=branch)
        return _fetch_workflow_from_runs(repo, workflow, branch, raw), raw

    return yaml_text, history.fetch_history(repo, workflow, runs=runs, branch=branch)


def _fetch_workflow_from_runs(
    repo: str,
    workflow: str,
    branch: str,
    raw: list[dict[str, Any]],
) -> str:
    sha = next((str(run["head_sha"]) for run in raw if run.get("head_sha")), "")
    if not sha:
        raise WorkflowNotFoundError.no_recorded_commit(repo, workflow, branch)
    try:
        return history.fetch_workflow(repo, workflow, ref=sha)
    except urllib.error.HTTPError as exc:
        if exc.code == _HTTP_NOT_FOUND:
            raise WorkflowNotFoundError.inaccessible_commit(repo, workflow, branch, sha) from exc
        raise


@tool(
    lane="analysis",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name whose Actions history to read"},
            "workflow": {"type": "string", "description": "File name under .github/workflows, e.g. ci.yml"},
            "runs": {"type": "integer", "description": "How many recent completed runs to read (default 150)"},
            "branch": {"type": "string", "description": "Only runs on this branch, e.g. main"},
            "workflow_content": {
                "type": "string",
                "description": "Full YAML text of .github/workflows/<workflow> from the sandbox (optional)",
            },
            "ref": {
                "type": "string",
                "description": "Git commit SHA or branch to read the workflow from when no local checkout exists",
            },
        },
        "required": ["repo", "workflow"],
    },
)
def profile_ci(  # noqa: PLR0913  optional workflow_content/ref allow remote sandbox profiling without a host checkout
    ledger: Ledger,  # noqa: ARG001  every tool takes the ledger first
    repo: str,
    workflow: str,
    runs: int = history.DEFAULT_RUNS,
    branch: str | None = None,
    *,
    workflow_content: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Profile one workflow from its recent successful runs.

    Returns the critical path (the chain of jobs that sets wall-clock time) with its time split
    into waiting (for a runner), setup (checkout, toolchain, installs, caches) and running (the
    checks themselves) at p50/p95, and biggest_bucket / biggest_bucket_p95: the one to fix first. Per job it returns
    the same split, how often the job was on the critical path, and max_saving_seconds: the most
    wall-clock time any change to that job can save. Jobs with max_saving_seconds of 0 are off
    the critical path; speeding them up saves nothing. Also returns the slowest steps.
    """
    return _profile_workflow(repo, workflow, runs, branch, workflow_content=workflow_content, ref=ref)[0].to_dict()


def _profile_workflow(
    repo: str,
    workflow: str,
    runs: int,
    branch: str | None,
    *,
    workflow_content: str | None = None,
    ref: str | None = None,
) -> tuple[CIProfile, Workflow]:
    """Validate the arguments, read the workflow file and its run history, and profile it."""
    if not _REPO.fullmatch(repo) or ".." in repo:
        msg = f"repo must be owner/name, got {repo!r}"
        raise ValueError(msg)
    yaml_text, raw = _load_workflow_and_history(
        repo,
        workflow,
        runs=runs,
        branch=branch,
        workflow_content=workflow_content,
        ref=ref,
    )
    spec = parse_workflow(yaml_text)
    return profile(spec, history.parse_runs(raw)), spec


def _uncovered(ci: CIProfile) -> list[str]:
    """Plain-language notes on time no playbook skill can shrink."""
    notes: list[str] = []
    if ci.runs_analyzed == 0:  # with nothing measured the buckets are all zero and "waiting" would win by default
        return ["No runs analyzed (all excluded or none fetched); nothing was measured."]
    if "waiting" in {ci.biggest_bucket, ci.biggest_bucket_p95}:
        notes.append(
            "The critical path's biggest bucket is waiting for a runner "
            f"(p50: {ci.biggest_bucket}, p95: {ci.biggest_bucket_p95}): no playbook skill shrinks runner queueing. "
            "Report it; runner choice and cancelling superseded runs are outside the playbook."
        )
    return notes


@tool(
    lane="analysis",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name whose Actions history to read"},
            "workflow": {"type": "string", "description": "File name under .github/workflows, e.g. ci.yml"},
            "runs": {"type": "integer", "description": "How many recent completed runs to read (default 150)"},
            "branch": {"type": "string", "description": "Only runs on this branch, e.g. main"},
        },
        "required": ["repo", "workflow"],
    },
)
def find_bottlenecks(
    ledger: Ledger,
    repo: str,
    workflow: str,
    runs: int = history.DEFAULT_RUNS,
    branch: str | None = None,
) -> dict[str, Any]:
    """Profile one workflow, run every bottleneck detector, and rank what is left.

    Returns profile_summary (wall-clock p50/p95, the critical path and its waiting/setup/running
    split, and base_sha: the commit of the newest analyzed run), findings (each ready to pass to
    record_hypothesis; biggest expected_savings_seconds first, none under 10 seconds; bottlenecks
    already in the ledger are left out) and uncovered (time no playbook skill can shrink, such as
    runner queueing: report these).
    """
    ci, spec = _profile_workflow(repo, workflow, runs, branch)
    path = f".github/workflows/{workflow}"
    seen = recorded_keys(ledger, repo)
    found = keep_worthwhile(f for detect in DETECTORS for f in detect(ci, spec, path) if f.key not in seen)
    summary = ci.to_dict()
    return {
        "profile_summary": {
            "base_sha": ci.newest_head_sha,
            **{
                key: summary[key]
                for key in (
                    "runs_analyzed",
                    "wall_clock_median_seconds",
                    "wall_clock_p95_seconds",
                    "critical_path",
                    "critical_path_buckets",
                    "biggest_bucket",
                    "biggest_bucket_p95",
                )
            },
        },
        "findings": [f.to_dict() for f in found],
        "uncovered": _uncovered(ci),
    }


class InvalidRecordError(ValueError):
    """Raised when record_hypothesis arguments are inconsistent. The managed-agent loop returns it to the model."""

    @classmethod
    def bad_workflow_path(cls, value: str) -> "InvalidRecordError":
        """Build error for a workflow_path outside .github/workflows."""
        return cls(f"workflow_path must be empty or like .github/workflows/ci.yml, got {value!r}")

    @classmethod
    def non_positive_savings(cls, value: float) -> "InvalidRecordError":
        """Build error for an expected saving of zero or less."""
        return cls(f"expected_savings_seconds must be greater than 0, got {value!r}")

    @classmethod
    def non_finite_savings(cls, value: float) -> "InvalidRecordError":
        """Build error for a nan or infinite expected saving."""
        return cls(f"expected_savings_seconds must be a finite number, got {value!r}")

    @classmethod
    def non_finite_max(cls, value: float) -> "InvalidRecordError":
        """Build error for a nan or infinite evidence max_saving_seconds."""
        return cls(f"evidence max_saving_seconds must be a finite number, got {value!r}")

    @classmethod
    def savings_above_max(cls, value: float, maximum: float) -> "InvalidRecordError":
        """Build error for an expected saving larger than the job can give."""
        return cls(f"expected_savings_seconds {value!r} exceeds evidence max_saving_seconds {maximum!r}")


def _check_record(workflow_path: str, expected_savings_seconds: float, evidence: dict[str, float | str]) -> None:
    if workflow_path and not _WORKFLOW_PATH.fullmatch(workflow_path):
        raise InvalidRecordError.bad_workflow_path(workflow_path)
    if not math.isfinite(expected_savings_seconds):
        raise InvalidRecordError.non_finite_savings(expected_savings_seconds)
    if expected_savings_seconds <= 0:
        raise InvalidRecordError.non_positive_savings(expected_savings_seconds)
    maximum = evidence.get("max_saving_seconds")
    if isinstance(maximum, int | float) and not math.isfinite(maximum):
        raise InvalidRecordError.non_finite_max(maximum)
    if isinstance(maximum, int | float) and expected_savings_seconds > maximum:
        raise InvalidRecordError.savings_above_max(expected_savings_seconds, maximum)


@tool(
    lane="analysis",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name of the fork"},
            "description": {"type": "string", "description": "The bottleneck, with the evidence for it"},
            "skill": {"type": "string", "description": "Playbook skill that should fix it"},
            "expected_savings_seconds": {
                "type": "number",
                "description": "Wall-clock seconds saved; above 0 and at most the job's max_saving_seconds",
            },
            "workflow_path": {"type": "string", "description": "e.g. .github/workflows/ci.yml"},
            "job": {"type": "string", "description": "Job id under jobs: in the workflow"},
            "step": {"type": "string", "description": "Step name within the job; empty for job-level changes"},
            "bucket": {"type": "string", "description": "Where the time goes: waiting, setup or running"},
            "evidence": {
                "type": "object",
                "description": "Measured numbers behind the claim, e.g. step_p50_seconds, max_saving_seconds",
                "additionalProperties": {"type": ["number", "string"]},
            },
            "confidence": {"type": "number", "description": "0 to 1: how sure you are the saving is real"},
            "risk": {"type": "string", "description": "Chance the change weakens what CI catches: low, medium, high"},
            "base_sha": {"type": "string", "description": "Commit the measurements were taken on"},
        },
        "required": ["repo", "description", "skill", "expected_savings_seconds"],
    },
)
def record_hypothesis(  # noqa: PLR0913, PLR0917  tool arguments mirror Hypothesis fields; the agent passes them by keyword
    ledger: Ledger,
    repo: str,
    description: str,
    skill: str,
    expected_savings_seconds: float,
    workflow_path: str = "",
    job: str = "",
    step: str = "",
    bucket: str = "",
    evidence: dict[str, float | str] | None = None,
    confidence: float | None = None,
    risk: str = "",
    base_sha: str = "",
) -> str:
    """Record one bottleneck worth fixing, with where to fix it. Returns the hypothesis id.

    Refuses a bottleneck (same skill, workflow_path, job and step) that is already recorded, whether
    it is open, rejected or kept: record a different one instead.
    """
    evidence = evidence or {}
    _check_record(workflow_path, expected_savings_seconds, evidence)
    if hypothesis_key(skill, workflow_path, job, step) in recorded_keys(ledger, repo):
        raise DuplicateHypothesisError.already_recorded(skill, workflow_path, job, step)
    hypothesis = Hypothesis(
        repo,
        description,
        skill,
        expected_savings_seconds,
        base_sha=base_sha,
        workflow_path=workflow_path,
        job=job,
        step=step,
        bucket=bucket,
        evidence=evidence,
        confidence=confidence,
        risk=risk,
    )
    ledger.add(hypothesis)
    return hypothesis.id
