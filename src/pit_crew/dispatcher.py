"""Orchestrates Pit Crew lane handoffs and enforces the candidate path safety guard.

Drives a single optimization cycle through ``analysis -> optimizer -> proof`` while
keeping all ledger state and trust boundaries on the host. Never imports ``google`` or
any lane module directly; lane execution goes through the injected ``LaneRunner``.
"""

import json
from dataclasses import asdict, dataclass
from importlib import import_module
from pathlib import PurePosixPath
from typing import Any, Protocol, cast, runtime_checkable

from pit_crew.contracts import (
    Candidate,
    Evidence,
    Hypothesis,
    Record,
    ReplayPlan,
    Verdict,
    is_allowed_candidate_path,
)
from pit_crew.github import GitHubClient
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


class Dispatcher:
    """Host-side orchestrator driving lane handoffs and enforcing candidate safety guards."""

    def __init__(
        self,
        ledger: Ledger,
        github: GitHubClient,
        lane_runner: LaneRunner | None = None,
        config: DispatcherConfig | None = None,
    ) -> None:
        """Initialize the Dispatcher with a ledger, GitHub client, lane runner, and config."""
        self.ledger = ledger
        self.github = github
        self.lane_runner: LaneRunner = lane_runner if lane_runner is not None else _default_lane_runner
        self.config = config if config is not None else DispatcherConfig()

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

    def collect_evidence(
        self,
        candidate: Candidate,
        replay_plan: ReplayPlan,  # noqa: ARG002  hook populated by interleaved timing & replay lanes
        base_sha: str,  # noqa: ARG002  hook populated by interleaved timing & replay lanes
        *,
        workflow: str | None = None,  # noqa: ARG002  hook populated by interleaved timing & replay lanes
    ) -> Evidence:
        """Collect CI timing and bug-replay evidence for ``candidate``.

        TODO(dispatcher owner): wire interleaved timing runs and bug replay execution.
        """
        return Evidence(
            candidate_id=candidate.id,
            baseline_seconds=(),
            candidate_seconds=(),
            bugs_total=0,
            bugs_caught=0,
        )

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
