"""Optimizer lane: make one change for a hypothesis and report it as a candidate.

The agent edits files in its sandbox and reports their new text. The host, which holds the
GitHub token, publishes the branch, so no secret enters the sandbox. Every reported file must
pass `contracts.is_allowed_candidate_path` and the "never check less" guard in `validate.py`.
"""

import re
from dataclasses import asdict
from typing import Any

from pit_crew.contracts import Candidate, Hypothesis, is_allowed_candidate_path
from pit_crew.lanes.optimizer.configs import check_config
from pit_crew.lanes.optimizer.validate import YAML_SUFFIXES, GuardReport, check_change
from pit_crew.ledger import Ledger
from pit_crew.tools import tool

BRANCH_PREFIX = "pc/"
MAX_CANDIDATE_FILES = 5  # one change per candidate; a real one touches a workflow and maybe a config
_BRANCH = re.compile(r"pc/[A-Za-z0-9][A-Za-z0-9._/-]*")


class CandidateRejectedError(ValueError):
    """Raised when a reported candidate breaks a lane rule. The message lists every problem."""

    def __init__(self, problems: list[str]) -> None:
        """Join ``problems`` into one message the agent can act on."""
        super().__init__("candidate rejected:\n- " + "\n- ".join(problems))


@tool(
    lane="optimizer",
    parameters={
        "type": "object",
        "properties": {"hypothesis_id": {"type": "string"}},
        "required": ["hypothesis_id"],
    },
)
def get_hypothesis(ledger: Ledger, hypothesis_id: str) -> dict[str, Any]:
    """The hypothesis to implement: repo, description, playbook skill, expected savings and where to change."""
    return asdict(ledger.get(Hypothesis, hypothesis_id))


@tool(
    lane="optimizer",
    parameters={
        "type": "object",
        "properties": {"repo": {"type": "string", "description": "owner/name of the fork"}},
        "required": ["repo"],
    },
)
def list_prior_attempts(ledger: Ledger, repo: str) -> list[dict[str, Any]]:
    """Everything already tried on this repo: each hypothesis, its candidate and the verdict with its reason.

    Read it before changing anything. Do not repeat a rejected approach unless you change what
    made it fail.
    """
    return ledger.prior_attempts(repo)


@tool(
    lane="optimizer",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative path, e.g. .github/workflows/ci.yml"},
            "before": {"type": "string", "description": "The file's original text; empty for a new file"},
            "after": {"type": "string", "description": "The file's text after your change"},
        },
        "required": ["path", "before", "after"],
    },
)
def check_workflow_change(ledger: Ledger, path: str, before: str, after: str) -> dict[str, Any]:  # noqa: ARG001  every tool takes the ledger first
    """Check one changed file against the lane rules before reporting it.

    Returns ok, problems and warnings. Problems block the candidate: a path you may not change,
    YAML that does not parse, or anything that makes CI check less (a dropped command, trigger,
    matrix value or artifact upload; continue-on-error; if: false; a wider timeout; retries;
    flags such as `|| true`, `-k` or `--exit-zero`). Config files are checked too: package.json
    scripts, pytest and coverage settings, tox and Makefile commands, Cargo test profiles, test
    runner configs (retries, ignores, failure recordings) and tsconfig strictness. Fix every
    problem, then check again.
    """
    return _guard(path, before, after).to_dict()


@tool(
    lane="optimizer",
    parameters={
        "type": "object",
        "properties": {
            "hypothesis_id": {"type": "string"},
            "branch": {"type": "string", "description": "Branch for the host to publish: pc/<hypothesis_id>-<skill>"},
            "summary": {"type": "string", "description": "What changed and why it should be faster"},
            "files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "Every changed file: repo-relative path to its full new text",
            },
            "original_files": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "The same paths mapped to their original text; empty string for a new file",
            },
            "base_sha": {
                "type": "string",
                "description": (
                    "Resolved cycle commit SHA the candidate was built atop (defaults to hypothesis base_sha)"
                ),
            },
        },
        "required": ["hypothesis_id", "branch", "summary", "files", "original_files"],
    },
)
def report_candidate(  # noqa: PLR0913  arguments mirror the tool's JSON schema
    ledger: Ledger,
    hypothesis_id: str,
    branch: str,
    summary: str,
    *,
    files: dict[str, str] | None = None,
    original_files: dict[str, str] | None = None,
    base_sha: str | None = None,
) -> str:
    """Report your one change for a hypothesis. The host publishes the branch. Returns the candidate id.

    Fails with every problem listed if the branch name, a path or the guard in
    check_workflow_change rejects it. Nothing is recorded until it passes.
    """
    hypothesis = ledger.get(Hypothesis, hypothesis_id)  # fail on an unknown hypothesis
    files = files or {}
    problems = _candidate_problems(branch, files, original_files or {})
    if problems:
        raise CandidateRejectedError(problems)
    candidate = Candidate(
        hypothesis_id,
        branch,
        summary,
        repo=hypothesis.repo,
        base_sha=base_sha or hypothesis.base_sha,
        files=dict(files),
    )
    ledger.add(candidate)
    return candidate.id


def _candidate_problems(branch: str, files: dict[str, str], originals: dict[str, str]) -> list[str]:
    problems = []
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".", "/", ".lock")):
        problems.append(f"branch must look like {BRANCH_PREFIX}<hypothesis_id>-<skill>, got {branch!r}")
    if len(files) > MAX_CANDIDATE_FILES:
        problems.append(f"changes {len(files)} files; one change per candidate, at most {MAX_CANDIDATE_FILES}")
    for path, after in sorted(files.items()):
        if path not in originals:
            problems.append(f"{path}: missing from original_files (use an empty string for a new file)")
            continue
        problems.extend(_guard(path, originals[path], after).problems)
    return problems


def _guard(path: str, before: str, after: str) -> GuardReport:
    if not is_allowed_candidate_path(path):
        return GuardReport(problems=[f"{path}: outside what Optimizer may change (.github/** and runner configs)"])
    if path.endswith(YAML_SUFFIXES):
        return check_change(path, before, after)
    return check_config(path, before, after)
