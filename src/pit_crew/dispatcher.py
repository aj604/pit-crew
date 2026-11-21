"""Orchestrates Pit Crew lane handoffs, interleaved timing runs, and safety guards.

Drives a single optimization cycle through ``analysis -> optimizer -> proof`` while
keeping all ledger state, workflow timing collection, Rater verdicts, and trust
boundaries on the host. Never imports ``google`` or any lane module directly; lane
execution goes through the injected ``LaneRunner``.
"""

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import PurePosixPath
from typing import Any, Protocol, cast, runtime_checkable

from pit_crew.contracts import (
    BugCase,
    Candidate,
    Evidence,
    Hypothesis,
    Record,
    ReplayPlan,
    Verdict,
    is_allowed_candidate_path,
)
from pit_crew.github import GitHubClient, WorkflowRunResult
from pit_crew.ledger import Ledger
from pit_crew.rater import rate

DISALLOWED_PATH_REASON = "disallowed path modified"


def next_lane(record: Record) -> str | None:
    """The lane a new ledger record starts, or None if it starts no agent work."""
    if isinstance(record, Hypothesis):
        return "optimizer"
    if isinstance(record, Candidate):
        return "proof"
    return None


@dataclass(frozen=True)
class DispatcherConfig:
    """Configuration for Dispatcher timing runs, compounding, polling, and PR automation."""

    timing_pairs: int = 5
    max_compounding_cycles: int = 5
    workflow_file: str = "ci.yml"
    run_timeout_seconds: float = 900.0
    poll_interval_seconds: float = 5.0
    auto_open_pr: bool = True


@runtime_checkable
class LaneRunner(Protocol):
    """Callable boundary for executing a single agent lane against a ledger."""

    def __call__(
        self,
        lane: str,
        task: str,
        ledger: Ledger | None = None,
        *,
        repo: str | None = None,
    ) -> object:
        """Run ``lane`` with ``task``, persisting any produced records into ``ledger``."""
        ...


def _default_lane_runner(
    lane: str,
    task: str,
    ledger: Ledger | None = None,
    *,
    repo: str | None = None,
) -> object:
    """Lazily resolve and invoke ``pit_crew.managed_agent.run_lane``."""
    module = import_module("pit_crew.managed_agent")
    runner = cast("LaneRunner", module.run_lane)
    return runner(lane, task, ledger, repo=repo)


def _workflow_for_hypothesis(hypothesis: Hypothesis, default_workflow: str) -> str:
    """Resolve the workflow filename for ``hypothesis``, falling back to ``default_workflow``."""
    if not hypothesis.workflow_path:
        return default_workflow
    return PurePosixPath(hypothesis.workflow_path).name or default_workflow


def _build_analysis_task(
    *,
    repo: str,
    base_sha: str,
    workflow_file: str,
    prior_attempts: list[dict[str, Any]],
) -> str:
    payload = {
        "repo": repo,
        "base_sha": base_sha,
        "workflow_file": workflow_file,
        "prior_attempts": prior_attempts,
    }
    return (
        f"Profile CI workflows for {repo} at base commit {base_sha} and record prioritized hypotheses.\n"
        f"Context:\n{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def _build_optimizer_task(
    *,
    repo: str,
    base_sha: str,
    hypothesis: Hypothesis,
    prior_attempts: list[dict[str, Any]],
) -> str:
    payload = {
        "repo": repo,
        "base_sha": base_sha,
        "hypothesis": asdict(hypothesis),
        "prior_attempts": prior_attempts,
    }
    return (
        f"Implement hypothesis {hypothesis.id} ({hypothesis.skill}) for {repo} atop {base_sha} "
        f"and report a single candidate.\n"
        f"Context:\n{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def _build_proof_task(*, repo: str, base_sha: str, candidate: Candidate) -> str:
    payload = {
        "repo": repo,
        "base_sha": base_sha,
        "candidate": asdict(candidate),
    }
    return (
        f"Select historical bug commits or mutations for candidate {candidate.id} on {repo} "
        f"and record a replay plan.\n"
        f"Context:\n{json.dumps(payload, indent=2, sort_keys=True)}"
    )


@dataclass
class _TimingSide:
    """Mutable per-side state for an interleaved baseline/candidate timing sequence."""

    ref: str
    durations: list[float]
    active: bool = True


@dataclass(frozen=True)
class _ReplayTarget:
    """Shared target metadata for executing bug replays against a candidate."""

    repo: str
    workflow: str
    candidate: Candidate


class Dispatcher:
    """Host-side orchestrator driving lane handoffs, timing measurement, and candidate safety guards."""

    def __init__(  # noqa: PLR0913
        self,
        ledger: Ledger,
        github: GitHubClient,
        lane_runner: LaneRunner | None = None,
        config: DispatcherConfig | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the Dispatcher with a ledger, GitHub client, lane runner, config, and clock hooks."""
        self.ledger = ledger
        self.github = github
        self.lane_runner: LaneRunner = lane_runner if lane_runner is not None else _default_lane_runner
        self.config = config if config is not None else DispatcherConfig()
        self._sleep = sleep
        self._monotonic = monotonic

    def resolve_base_sha(self, repo: str, base_sha: str | None = None) -> str:
        """Resolve the commit SHA to optimize atop for ``repo``.

        Uses an explicit ``base_sha`` when supplied, otherwise advances from the latest
        kept candidate's ``head_sha`` in the ledger for compounding, and falls back to
        the repository's default branch SHA via ``GitHubClient``.
        """
        if base_sha:
            return base_sha
        latest = self.ledger.latest_kept_candidate(repo)
        if latest is not None and latest.head_sha:
            return latest.head_sha
        return self.github.get_default_branch_sha(repo)

    def select_next_hypothesis(self, repo: str) -> Hypothesis | None:
        """Return the highest-impact unattempted ``Hypothesis`` for ``repo``, or ``None``."""
        attempted_ids = {cand.hypothesis_id for cand in self.ledger.all(Candidate)}
        unattempted = [hyp for hyp in self.ledger.hypotheses_for_repo(repo) if hyp.id not in attempted_ids]
        if not unattempted:
            return None
        return max(unattempted, key=lambda hyp: hyp.expected_savings_seconds)

    def validate_candidate_paths(self, candidate: Candidate) -> tuple[str, ...]:
        """Return any disallowed file paths touched by ``candidate``."""
        paths = dict.fromkeys((*candidate.changed_files, *candidate.files))
        return tuple(path for path in paths if not is_allowed_candidate_path(path))

    def poll_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
        """Poll ``github.get_workflow_run`` until ``run_id`` completes or times out."""
        start = self._monotonic()
        while True:
            run = self.github.get_workflow_run(repo, run_id)
            if run.status == "completed":
                return run
            elapsed = self._monotonic() - start
            if elapsed >= self.config.run_timeout_seconds:
                msg = (
                    f"Workflow run {run_id} in {repo} did not complete within "
                    f"{self.config.run_timeout_seconds}s (last status: {run.status!r})"
                )
                raise TimeoutError(msg)
            self._sleep(self.config.poll_interval_seconds)

    def collect_evidence(
        self,
        candidate: Candidate,
        replay_plan: ReplayPlan,
        base_sha: str,
        *,
        workflow: str | None = None,
    ) -> Evidence:
        """Collect interleaved CI timing and bug-replay evidence for ``candidate`` against ``base_sha``.

        Alternates baseline (``base_sha``) and candidate (``candidate.branch`` or
        ``candidate.head_sha``) workflow dispatches for ``config.timing_pairs`` pairs,
        including cold Run #1 to cancel runner-load drift. Aborts timing collection for a
        side if any timing run concludes with a status other than ``"success"``.

        Then replays each ``kind == "commit"`` ``BugCase`` in ``replay_plan.cases`` by
        overlaying ``candidate.files`` onto ``bug_case.commit_sha`` on a temporary
        ``pc/replay-<candidate_id>-<idx>`` branch and recording whether CI catches the
        bug (concludes ``"failure"``).
        """
        repo = self._resolve_repo(candidate)
        workflow_file = self._resolve_workflow(candidate, workflow)
        candidate_ref = candidate.branch or candidate.head_sha
        if not candidate_ref:
            msg = f"Candidate {candidate.id!r} has neither branch nor head_sha"
            raise ValueError(msg)

        baseline_seconds, candidate_seconds, timing_urls = self._collect_interleaved_timings(
            repo=repo,
            workflow=workflow_file,
            base_sha=base_sha,
            candidate_ref=candidate_ref,
        )
        target = _ReplayTarget(repo=repo, workflow=workflow_file, candidate=candidate)
        bugs_total, bugs_caught, replay_urls, bug_outcomes = self._collect_bug_replays(target, replay_plan)
        return Evidence(
            candidate_id=candidate.id,
            baseline_seconds=baseline_seconds,
            candidate_seconds=candidate_seconds,
            bugs_total=bugs_total,
            bugs_caught=bugs_caught,
            run_urls=(*timing_urls, *replay_urls),
            bug_outcomes=bug_outcomes,
        )

    def _find_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        try:
            return self.ledger.get(Hypothesis, hypothesis_id)
        except KeyError:
            return None

    def _resolve_repo(self, candidate: Candidate) -> str:
        if candidate.repo:
            return candidate.repo
        hypothesis = self._find_hypothesis(candidate.hypothesis_id)
        if hypothesis is not None and hypothesis.repo:
            return hypothesis.repo
        msg = f"Cannot resolve repository for candidate {candidate.id!r}"
        raise ValueError(msg)

    def _resolve_workflow(self, candidate: Candidate, workflow: str | None) -> str:
        if workflow:
            return PurePosixPath(workflow).name or self.config.workflow_file
        hypothesis = self._find_hypothesis(candidate.hypothesis_id)
        if hypothesis is not None:
            return _workflow_for_hypothesis(hypothesis, self.config.workflow_file)
        return self.config.workflow_file

    def _dispatch_and_poll(self, repo: str, workflow: str, ref: str) -> WorkflowRunResult:
        run_id = self.github.dispatch_workflow(repo, workflow, ref)
        return self.poll_workflow_run(repo, run_id)

    def _collect_interleaved_timings(
        self,
        *,
        repo: str,
        workflow: str,
        base_sha: str,
        candidate_ref: str,
    ) -> tuple[tuple[float, ...], tuple[float, ...], tuple[str, ...]]:
        baseline = _TimingSide(ref=base_sha, durations=[])
        candidate = _TimingSide(ref=candidate_ref, durations=[])
        run_urls: list[str] = []

        for _ in range(self.config.timing_pairs):
            for side in (baseline, candidate):
                if side.active:
                    self._record_timing_run(repo, workflow, side, run_urls)
            if not baseline.active and not candidate.active:
                break

        return tuple(baseline.durations), tuple(candidate.durations), tuple(run_urls)

    def _record_timing_run(
        self,
        repo: str,
        workflow: str,
        side: _TimingSide,
        run_urls: list[str],
    ) -> None:
        run = self._dispatch_and_poll(repo, workflow, side.ref)
        if run.html_url:
            run_urls.append(run.html_url)
        if run.conclusion == "success":
            side.durations.append(run.duration_seconds)
        else:
            side.active = False

    def _collect_bug_replays(
        self,
        target: _ReplayTarget,
        replay_plan: ReplayPlan,
    ) -> tuple[int, int, tuple[str, ...], tuple[dict[str, Any], ...]]:
        bugs_total = 0
        bugs_caught = 0
        run_urls: list[str] = []
        bug_outcomes: list[dict[str, Any]] = []

        for idx, bug_case in enumerate(replay_plan.cases):
            # TODO(dispatcher owner): wire mutation bug replay execution alongside commit replays.
            if bug_case.kind != "commit":
                continue
            caught, outcome, run_url = self._replay_commit_case(target, bug_case, idx)
            bugs_total += 1
            if caught:
                bugs_caught += 1
            if run_url:
                run_urls.append(run_url)
            bug_outcomes.append(outcome)

        return bugs_total, bugs_caught, tuple(run_urls), tuple(bug_outcomes)

    def _replay_commit_case(
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
    ) -> tuple[bool, dict[str, Any], str]:
        commit_sha = bug_case.commit_sha
        if not commit_sha:
            msg = f"Commit bug case at index {idx} is missing commit_sha"
            raise ValueError(msg)

        replay_branch = f"pc/replay-{target.candidate.id}-{idx}"
        self.github.create_branch(target.repo, replay_branch, commit_sha)
        try:
            self.github.commit_files(
                target.repo,
                replay_branch,
                commit_sha,
                dict(target.candidate.files),
                f"Pit Crew replay: overlay candidate {target.candidate.id} onto {commit_sha}",
            )
            run = self._dispatch_and_poll(target.repo, target.workflow, replay_branch)
        finally:
            self.github.delete_branch(target.repo, replay_branch)

        caught = run.conclusion == "failure"
        outcome: dict[str, Any] = {
            "kind": "commit",
            "ref": commit_sha,
            "caught": caught,
            "baseline_failed": True,
            "run_url": run.html_url,
        }
        return caught, outcome, run.html_url

    def run_cycle(self, repo: str, base_sha: str | None = None) -> Verdict | None:
        """Run one optimization cycle for ``repo`` and return the resulting ``Verdict``, if any."""
        resolved_base_sha = self.resolve_base_sha(repo, base_sha)

        analysis_task = _build_analysis_task(
            repo=repo,
            base_sha=resolved_base_sha,
            workflow_file=self.config.workflow_file,
            prior_attempts=self.ledger.prior_attempts(repo),
        )
        self.lane_runner("analysis", analysis_task, self.ledger, repo=repo)

        hypothesis = self.select_next_hypothesis(repo)
        if hypothesis is None:
            return None

        candidate = self._run_optimizer(repo, resolved_base_sha, hypothesis)
        if candidate is None:
            return None

        if self.validate_candidate_paths(candidate):
            verdict = Verdict(
                candidate_id=candidate.id,
                keep=False,
                speedup=0.0,
                p_value=None,
                reason=DISALLOWED_PATH_REASON,
            )
            self.ledger.add(verdict)
            return verdict

        replay_plan = self._run_proof(repo, resolved_base_sha, candidate)
        if replay_plan is None:
            return None

        workflow = _workflow_for_hypothesis(hypothesis, self.config.workflow_file)
        evidence = self.collect_evidence(candidate, replay_plan, resolved_base_sha, workflow=workflow)
        self.ledger.add(evidence)
        verdict = rate(evidence)
        self.ledger.add(verdict)
        return verdict

    def _run_optimizer(self, repo: str, base_sha: str, hypothesis: Hypothesis) -> Candidate | None:
        existing_ids = {cand.id for cand in self.ledger.all(Candidate)}
        task = _build_optimizer_task(
            repo=repo,
            base_sha=base_sha,
            hypothesis=hypothesis,
            prior_attempts=self.ledger.prior_attempts(repo),
        )
        self.lane_runner("optimizer", task, self.ledger, repo=repo)
        new_candidates = [
            cand
            for cand in self.ledger.all(Candidate)
            if cand.id not in existing_ids and cand.hypothesis_id == hypothesis.id
        ]
        return new_candidates[-1] if new_candidates else None

    def _run_proof(self, repo: str, base_sha: str, candidate: Candidate) -> ReplayPlan | None:
        existing_ids = {plan.id for plan in self.ledger.all(ReplayPlan)}
        task = _build_proof_task(repo=repo, base_sha=base_sha, candidate=candidate)
        self.lane_runner("proof", task, self.ledger, repo=repo)
        new_plans = [
            plan
            for plan in self.ledger.all(ReplayPlan)
            if plan.id not in existing_ids and plan.candidate_id == candidate.id
        ]
        return new_plans[-1] if new_plans else None

    def run_until_sla(self, repo: str) -> list[Verdict]:
        """Run compounding optimization cycles for ``repo`` until the CI SLA is met or cycles are exhausted.

        Repeatedly invokes ``run_cycle(repo)`` up to ``config.max_compounding_cycles`` iterations,
        collecting each non-``None`` ``Verdict``. Each subsequent cycle automatically advances
        ``base_sha`` to ``ledger.latest_kept_candidate(repo).head_sha`` after a kept candidate,
        while retaining the previous ``base_sha`` after a rejected candidate. Stops early when
        ``run_cycle(repo)`` returns ``None`` or a kept verdict satisfies the CI SLA.
        """
        verdicts: list[Verdict] = []
        for _ in range(self.config.max_compounding_cycles):
            verdict = self.run_cycle(repo)
            if verdict is None:
                break
            verdicts.append(verdict)
            if verdict.keep and verdict.sla_met:
                break
        return verdicts
