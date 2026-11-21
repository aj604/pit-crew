"""Analysis lane: find where the minutes go and record hypotheses.

Pure logic lives in `history.py` (fetch + parse run history), `workflow.py` (job graph) and
`critical_path.py` (profiling). This module only wraps it as agent tools.
"""

import math
import os
import re
from pathlib import Path
from typing import Any

from pit_crew.contracts import Hypothesis
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.critical_path import profile
from pit_crew.lanes.analysis.dedupe_ledger import DuplicateHypothesisError, hypothesis_key, recorded_keys
from pit_crew.lanes.analysis.workflow import parse_workflow
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW_FILE = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml")


def workspace() -> Path:
    """Where the fork is checked out: /workspace in the managed-agent sandbox."""
    return Path(os.environ.get("PIT_CREW_WORKSPACE", "/workspace"))


def workflow_path(workflow: str) -> Path:
    """The workflow file inside the workspace. Rejects anything but a bare .yml/.yaml file name."""
    if not _WORKFLOW_FILE.fullmatch(workflow) or workflow.startswith("."):
        msg = f"workflow must be a file name like ci.yml, got {workflow!r}"
        raise ValueError(msg)
    folder = (workspace() / ".github" / "workflows").resolve()
    path = (folder / workflow).resolve()
    if path.parent != folder:  # a symlink pointing out of the folder
        msg = f"workflow {workflow!r} resolves outside .github/workflows"
        raise ValueError(msg)
    return path


def _read_workflow(repo: str, workflow: str, *, branch: str | None = None) -> str:
    """Read a workflow from the local checkout when present, else fetch it from GitHub."""
    path = workflow_path(workflow)
    if path.is_file():
        return path.read_text()
    return history.fetch_workflow(repo, workflow, ref=branch)


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
def profile_ci(
    ledger: Ledger,  # noqa: ARG001  every tool takes the ledger first
    repo: str,
    workflow: str,
    runs: int = history.DEFAULT_RUNS,
    branch: str | None = None,
) -> dict[str, Any]:
    """Profile one workflow from its recent successful runs.

    Returns the critical path (the chain of jobs that sets wall-clock time) with its time split
    into waiting (for a runner), setup (checkout, toolchain, installs, caches) and running (the
    checks themselves) at p50/p95, and biggest_bucket / biggest_bucket_p95: the one to fix first. Per job it returns
    the same split, how often the job was on the critical path, and max_saving_seconds: the most
    wall-clock time any change to that job can save. Jobs with max_saving_seconds of 0 are off
    the critical path; speeding them up saves nothing. Also returns the slowest steps.
    """
    if not _REPO.fullmatch(repo) or ".." in repo:
        msg = f"repo must be owner/name, got {repo!r}"
        raise ValueError(msg)
    spec = parse_workflow(_read_workflow(repo, workflow, branch=branch))
    raw = history.fetch_history(repo, workflow, runs=runs, branch=branch)
    return profile(spec, history.parse_runs(raw)).to_dict()


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
