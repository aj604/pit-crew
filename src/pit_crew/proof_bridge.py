"""Deterministic pre-pass coordinator that injects Proof context into Proof lane prompts.

Before the Proof lane agent runs, ``ProofBridge`` diffs original workflow files at ``base_sha``
against ``Candidate.files`` to identify ``touched_checks``, mines deterministic ``red_commits``
from GitHub Actions job runs, computes ``required_replays`` and ``uncovered_checks``, and embeds
them into the Proof lane task prompt.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from pit_crew.contracts import Candidate, Evidence, Verdict
from pit_crew.github import PullRequestClient
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.checks import Check, FileChange, touched_checks
from pit_crew.lanes.proof.miner import JobRun, RedCommit, flaky_checks, red_commits
from pit_crew.lanes.proof.tools import MAX_REPLAYS, pick_replays, uncovered_checks
from pit_crew.lanes.proof.verify import AskModel, Replay, compare_failures, tally, verify_replays
from pit_crew.ledger import Ledger

PROOF_CARD_PATH = ".github/pit-crew/proof-card.svg"


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

    @classmethod
    def missing_verifier(cls) -> "ProofBridgeError":
        """Build error when ``verify_replay_runs()`` is called without a multimodal ``ask`` callable."""
        return cls("ProofBridge.verify_replay_runs() requires an ask callable (e.g. managed_agent.ask_multimodal)")

    @classmethod
    def missing_pr_client(cls) -> "ProofBridgeError":
        """Build error when ``open_kept_pr()`` is called without a ``PullRequestClient``."""
        return cls("ProofBridge.open_kept_pr() requires a PullRequestClient (github or pr_client)")


class ProofHistoryClient(Protocol):
    """Reads base files and recent workflow job runs for Proof pre-pass mining."""

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        """Return UTF-8 file content at ``ref`` in ``repo``, or ``None`` if absent."""
        ...

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        """Return recent GitHub Actions job run attempts for ``repo``."""
        ...

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        """Return failed job logs or recorded artifact bytes for workflow run ``run_id`` in ``repo``."""
        ...


class LaneRunner(Protocol):
    """Callable that invokes a lane with a task prompt (e.g. ``managed_agent.run_lane``)."""

    def __call__(self, lane: str, task: str, *, repo: str = "", ledger: Ledger | None = None) -> object:
        """Execute ``lane`` with ``task`` and return the lane interaction/result."""
        ...


@dataclass(frozen=True)
class ReplayRunPair:
    """One bug replay pair measured on baseline and candidate workflow runs."""

    sha: str
    check: str
    baseline_conclusion: str
    candidate_conclusion: str
    baseline_run_id: str
    candidate_run_id: str


@dataclass(frozen=True)
class ReplayVerification:
    """Verified replay outcomes ready to populate ``Evidence``."""

    replays: tuple[Replay, ...]
    bugs_total: int
    bugs_caught: int
    bug_outcomes: tuple[dict[str, object], ...]

    def to_evidence(
        self,
        candidate_id: str,
        baseline_seconds: tuple[float, ...],
        candidate_seconds: tuple[float, ...],
        *,
        run_urls: tuple[str, ...] = (),
    ) -> Evidence:
        """Build a ``contracts.Evidence`` record from these verified replay outcomes."""
        return Evidence(
            candidate_id=candidate_id,
            baseline_seconds=baseline_seconds,
            candidate_seconds=candidate_seconds,
            bugs_total=self.bugs_total,
            bugs_caught=self.bugs_caught,
            run_urls=run_urls,
            bug_outcomes=self.bug_outcomes,
        )


@dataclass(frozen=True)
class ProofPRConfig:
    """Configuration for Proof Card attachment and PR creation on kept candidates."""

    auto_open_pr: bool = False
    base_branch: str = "main"
    repo: str = ""


@dataclass(frozen=True)
class OpenedPullRequest:
    """Metadata for a pull request opened with a committed SVG Proof Card."""

    url: str
    card_path: str
    card_sha: str
    card_url: str
    title: str
    body: str


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
        ask: AskModel | None = None,
    ) -> None:
        """Create a bridge using ``github`` for history/base files, optional ``run_lane``, and optional ``ask``."""
        self._github = github
        self._run_lane = run_lane
        self._replay_limit = replay_limit
        self._ask = ask

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

    def verify_replay_runs(
        self,
        repo: str,
        runs: Iterable[ReplayRunPair],
        *,
        ask: AskModel | None = None,
    ) -> ReplayVerification:
        """Verify failure equivalence for replay pairs where both baseline and candidate failed.

        Fetches failed run output (logs / artifact bytes) from GitHub for each failing pair and
        delegates multimodal comparison to ``compare_failures(..., ask=ask)``. Any ``"different failure"``
        or ``"unverified"`` outcome fails closed and does not increment ``bugs_caught``.
        """
        verifier = ask if ask is not None else self._ask
        if verifier is None:
            raise ProofBridgeError.missing_verifier()

        raw_replays = [
            Replay(
                sha=pair.sha,
                check=pair.check,
                baseline_failed=pair.baseline_conclusion == "failure",
                candidate_failed=pair.candidate_conclusion == "failure",
                baseline_video=pair.baseline_run_id,
                candidate_video=pair.candidate_run_id,
            )
            for pair in runs
        ]
        verified = tuple(
            verify_replays(
                raw_replays,
                load=lambda run_id: self._github.get_run_failure_output(repo, run_id),
                compare=partial(compare_failures, ask=verifier),
            )
        )
        bugs_total, bugs_caught = tally(verified)
        return ReplayVerification(
            replays=verified,
            bugs_total=bugs_total,
            bugs_caught=bugs_caught,
            bug_outcomes=tuple(replay.as_outcome() for replay in verified),
        )

    def open_kept_pr(
        self,
        candidate: Candidate,
        evidence: Evidence,
        verdict: Verdict,
        replays: Sequence[Replay],
        *,
        config: ProofPRConfig | None = None,
    ) -> OpenedPullRequest | None:
        """Render and commit the SVG Proof Card and open a PR when ``verdict.keep`` and ``config.auto_open_pr``."""
        effective = config or ProofPRConfig()
        if not verdict.keep or not effective.auto_open_pr:
            return None
        target_repo = effective.repo or candidate.repo
        if not target_repo:
            raise ProofBridgeError.missing_repo(candidate.id)
        if not isinstance(self._github, PullRequestClient):
            raise ProofBridgeError.missing_pr_client()

        svg = render_card(candidate.branch, evidence, verdict, replays)
        card_sha = self._github.commit_files(
            target_repo,
            candidate.branch,
            {PROOF_CARD_PATH: svg},
            f"pit-crew: add proof card for {candidate.id}",
        )
        card_url = f"https://raw.githubusercontent.com/{target_repo}/{card_sha}/{PROOF_CARD_PATH}"
        body = pr_body(candidate.branch, evidence, verdict, replays, card_url=card_url)
        title = _pr_title(candidate)
        url = self._github.open_pull_request(
            target_repo,
            head=candidate.branch,
            base=effective.base_branch,
            title=title,
            body=body,
        )
        return OpenedPullRequest(
            url=url,
            card_path=PROOF_CARD_PATH,
            card_sha=card_sha,
            card_url=card_url,
            title=title,
            body=body,
        )

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


def _pr_title(candidate: Candidate) -> str:
    subject = candidate.summary.strip().splitlines()[0] if candidate.summary.strip() else candidate.branch
    return f"pit-crew: {subject}"


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
