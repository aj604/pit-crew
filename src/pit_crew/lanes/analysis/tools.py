"""Analysis lane: find where the minutes go and record hypotheses.

Pure logic lives in `history.py` (fetch + parse run history), `workflow.py` (job graph) and
`critical_path.py` (profiling). This module only wraps it as agent tools.
"""

import os
import re
from pathlib import Path
from typing import Any

from pit_crew.contracts import Hypothesis
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis.critical_path import profile
from pit_crew.lanes.analysis.workflow import parse_workflow
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_WORKFLOW_FILE = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")


def workspace() -> Path:
    """Where the fork is checked out: /workspace in the managed-agent sandbox."""
    return Path(os.environ.get("PIT_CREW_WORKSPACE", "/workspace"))


def workflow_path(workflow: str) -> Path:
    """The workflow file inside the workspace. Rejects anything but a bare .yml/.yaml file name."""
    if not _WORKFLOW_FILE.fullmatch(workflow) or workflow.startswith("."):
        raise ValueError(f"workflow must be a file name like ci.yml, got {workflow!r}")
    folder = (workspace() / ".github" / "workflows").resolve()
    path = (folder / workflow).resolve()
    if path.parent != folder:  # a symlink pointing out of the folder
        raise ValueError(f"workflow {workflow!r} resolves outside .github/workflows")
    return path


@tool(
    lane="analysis",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name whose Actions history to read"},
            "workflow": {"type": "string", "description": "File name under .github/workflows, e.g. ci.yml"},
            "runs": {"type": "integer", "description": "How many recent completed runs to read (default 30)"},
            "branch": {"type": "string", "description": "Only runs on this branch, e.g. main"},
        },
        "required": ["repo", "workflow"],
    },
)
def profile_ci(ledger: Ledger, repo: str, workflow: str, runs: int = 30, branch: str | None = None) -> dict[str, Any]:
    """Profile one workflow from its recent successful runs.

    Returns the critical path (the chain of jobs that sets wall-clock time), and per job its
    median/p95 time split into runner time and queue time, how often it was on the critical
    path, and max_saving_seconds: the most wall-clock time any change to that job can save.
    Jobs with max_saving_seconds of 0 are off the critical path; speeding them up saves nothing.
    Also returns the slowest steps.
    """
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise ValueError(f"repo must be owner/name, got {repo!r}")
    spec = parse_workflow(workflow_path(workflow).read_text())
    raw = history.fetch_history(repo, workflow, runs=runs, branch=branch)
    return profile(spec, history.parse_runs(raw)).to_dict()


@tool(
    lane="analysis",
    parameters={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "owner/name of the fork"},
            "description": {"type": "string", "description": "The bottleneck, with the evidence for it"},
            "skill": {"type": "string", "description": "Playbook skill that should fix it"},
            "expected_savings_seconds": {"type": "number"},
        },
        "required": ["repo", "description", "skill", "expected_savings_seconds"],
    },
)
def record_hypothesis(ledger: Ledger, repo: str, description: str, skill: str, expected_savings_seconds: float) -> str:
    """Record one bottleneck worth fixing. Returns the hypothesis id."""
    hypothesis = Hypothesis(repo, description, skill, expected_savings_seconds)
    ledger.add(hypothesis)
    return hypothesis.id
