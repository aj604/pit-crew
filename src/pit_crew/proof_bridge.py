"""Deterministic pre-pass coordinator that injects Proof context into Proof lane prompts.

Before the Proof lane agent runs, ``ProofBridge`` diffs original workflow files at ``base_sha``
against ``Candidate.files`` to identify ``touched_checks``, mines deterministic ``red_commits``
from GitHub Actions job runs, computes ``required_replays`` and ``uncovered_checks``, and embeds
them into the Proof lane task prompt.
"""

from dataclasses import dataclass
from typing import Protocol

from pit_crew.contracts import Candidate
from pit_crew.github import ProofHistoryClient
from pit_crew.lanes.proof.checks import Check, FileChange, touched_checks
from pit_crew.lanes.proof.miner import RedCommit, flaky_checks, red_commits
from pit_crew.lanes.proof.tools import MAX_REPLAYS, pick_replays, uncovered_checks
from pit_crew.ledger import Ledger


class ProofBridgeError(ValueError):
    """Raised when ``ProofBridge`` cannot prepare context or invoke the Proof lane."""

    @classmethod
    def missing_repo(cls, candidate_id: str) -> "ProofBridgeError":
        """Build error when neither ``candidate.repo`` nor ``repo`` is provided."""
        return cls(f"Candidate {candidate_id} has no repo; pass repo explicitly or set Candidate.repo")

    @classmethod
    def missing_runner(cls) -> "ProofBridgeError":
        """Build error when ``run()`` is called without a configured ``run_lane`` callable."""
        return cls("ProofBridge.run() requires a run_lane callable")


class LaneRunner(Protocol):
    """Callable that invokes a lane with a task prompt (e.g. ``managed_agent.run_lane``)."""

    def __call__(self, lane: str, task: str, *, repo: str = "", ledger: Ledger | None = None) -> object:
        """Execute ``lane`` with ``task`` and return the lane interaction/result."""
        ...


@dataclass(frozen=True)
class ProofContext:
    """Deterministic pre-Proof analysis computed before invoking the Proof lane agent."""

    candidate: Candidate
    repo: str
    base_sha: str
    touched_checks: frozenset[Check] | None
    red_commits: tuple[RedCommit, ...]
    flaky_checks: tuple[tuple[str, str], ...]
    required_replays: tuple[str, ...]
    uncovered_checks: frozenset[Check]


class ProofBridge:
    """Computes deterministic pre-Proof context and injects it into Proof lane prompts."""

    def __init__(
        self,
        github: ProofHistoryClient,
        run_lane: LaneRunner | None = None,
        *,
        replay_limit: int = MAX_REPLAYS,
    ) -> None:
        """Create a bridge using ``github`` for history/base files and optional ``run_lane``."""
        self._github = github
        self._run_lane = run_lane
        self._replay_limit = replay_limit

    def prepare_context(
        self,
        candidate: Candidate,
        *,
        repo: str = "",
        base_sha: str = "",
    ) -> ProofContext:
        """Compute ``touched_checks``, ``red_commits``, ``required_replays``, and ``uncovered_checks``."""
        target_repo = repo or candidate.repo
        if not target_repo:
            raise ProofBridgeError.missing_repo(candidate.id)
        target_base = base_sha or candidate.base_sha

        touched = self._compute_touched_checks(candidate, target_repo, target_base)
        job_runs = list(self._github.list_workflow_job_runs(target_repo))
        mined_red = tuple(red_commits(job_runs))
        flaky = tuple(flaky_checks(job_runs))
        required = tuple(pick_replays(mined_red, touched, limit=self._replay_limit))
        uncovered = frozenset(uncovered_checks(mined_red, touched)) if touched is not None else frozenset()

        return ProofContext(
            candidate=candidate,
            repo=target_repo,
            base_sha=target_base,
            touched_checks=None if touched is None else frozenset(touched),
            red_commits=mined_red,
            flaky_checks=flaky,
            required_replays=required,
            uncovered_checks=uncovered,
        )

    def build_prompt(self, context: ProofContext, *, extra_instructions: str = "") -> str:
        """Render the Proof lane task prompt with pre-computed deterministic context embedded."""
        candidate = context.candidate
        changed_files = ", ".join(candidate.changed_files) if candidate.changed_files else "(none)"
        sections = [
            f"Build a ReplayPlan for Candidate `{candidate.id}`.",
            "",
            "## Candidate",
            f"- candidate_id: {candidate.id}",
            f"- repo: {context.repo}",
            f"- branch: {candidate.branch}",
            f"- base_sha: {context.base_sha or '(unknown)'}",
            f"- head_sha: {candidate.head_sha or '(unpublished)'}",
            f"- summary: {candidate.summary}",
            f"- changed_files: {changed_files}",
            "",
            "## Touched checks",
            _format_checks(context.touched_checks),
            "",
            "## Required replays",
            _format_shas(context.required_replays),
            "",
            "## Uncovered checks",
            _format_uncovered(context.touched_checks, context.uncovered_checks),
            "",
            "## Red commits (deterministic failures, newest first)",
            _format_red_commits(context.red_commits),
        ]
        if context.flaky_checks:
            sections.extend(
                [
                    "",
                    "## Excluded flaky checks",
                    *(f"- {sha} ({check})" for sha, check in context.flaky_checks),
                ]
            )
        if extra_instructions.strip():
            sections.extend(["", "## Additional instructions", extra_instructions.strip()])
        sections.extend(
            [
                "",
                "## Instructions",
                (
                    f"Keep every required replay above and call `plan_replays(candidate_id={candidate.id!r}, ...)` "
                    "with `bug_shas` and synthetic `mutations` covering each uncovered check."
                ),
            ]
        )
        return "\n".join(sections)

    def run(
        self,
        candidate: Candidate,
        *,
        ledger: Ledger | None = None,
        repo: str = "",
        base_sha: str = "",
        extra_instructions: str = "",
    ) -> tuple[ProofContext, object]:
        """Prepare pre-Proof context, inject it into the prompt, and invoke the Proof lane."""
        if self._run_lane is None:
            raise ProofBridgeError.missing_runner()
        context = self.prepare_context(candidate, repo=repo, base_sha=base_sha)
        prompt = self.build_prompt(context, extra_instructions=extra_instructions)
        result = self._run_lane("proof", prompt, repo=context.repo, ledger=ledger)
        return context, result

    def _compute_touched_checks(
        self,
        candidate: Candidate,
        repo: str,
        base_sha: str,
    ) -> set[Check] | None:
        if not candidate.files:
            return None if candidate.changed_files else set()
        changes: list[FileChange] = []
        for path, after in sorted(candidate.files.items()):
            before = self._github.get_file_content(repo, base_sha, path) if base_sha else None
            changes.append(FileChange(path=path, before=before, after=after))
        return touched_checks(changes)


def _format_check(check: Check) -> str:
    return " | ".join(sorted(check))


def _format_checks(checks: frozenset[Check] | None) -> str:
    if checks is None:
        return "- ALL checks (non-workflow file or unparseable workflow changed)"
    if not checks:
        return "- (none)"
    return "\n".join(f"- {_format_check(check)}" for check in sorted(checks, key=_format_check))


def _format_shas(shas: tuple[str, ...]) -> str:
    if not shas:
        return "- (none)"
    return "\n".join(f"- {sha}" for sha in shas)


def _format_uncovered(touched: frozenset[Check] | None, uncovered: frozenset[Check]) -> str:
    if touched is None:
        return "- Inspect repository workflows to identify checks without a recorded red commit."
    if not uncovered:
        return "- (none)"
    return "\n".join(f"- {_format_check(check)}" for check in sorted(uncovered, key=_format_check))


def _format_red_commits(commits: tuple[RedCommit, ...]) -> str:
    if not commits:
        return "- (none)"
    lines = []
    for commit in commits:
        status = "fixed" if commit.fixed else "unfixed"
        checks = ", ".join(commit.checks)
        lines.append(f"- {commit.sha} [{status}] ({commit.branch}, {commit.created_at}): {checks}")
    return "\n".join(lines)
