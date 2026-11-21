"""Orchestrates Pit Crew lane handoffs, interleaved timing runs, and safety guards.

Drives a single optimization cycle through ``analysis -> optimizer -> proof`` while
keeping all ledger state, workflow timing collection, Rater verdicts, and trust
boundaries on the host. Never imports ``google`` or any lane module directly; lane
execution goes through the injected ``LaneRunner``.
"""

import difflib
import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
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
from pit_crew.ledger import Ledger, normalize_repo
from pit_crew.rater import rate

DISALLOWED_PATH_REASON = "disallowed path modified"
NO_REPLAY_PLAN_REASON = "no replay plan produced, so safety is unproven"

_Hunk = tuple[int, int, list[str]]


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
    base_branch: str = "main"


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


def _is_completed_lane_result(result: object) -> bool:
    """Return True when a lane runner returned a definitively completed interaction."""
    return getattr(result, "status", None) == "completed"


def _is_allowed_mutation_path(path: str) -> bool:
    """Return True when Proof is permitted to mutate ``path`` during replay.

    Rejects invalid relative paths and anything under ``.github/`` so untrusted
    repository content cannot inject or replace GitHub Actions workflows.
    """
    if not path or path.startswith("/") or "\\" in path:
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    return parts[0].lower() != ".github"


def _change_hunks(base_lines: list[str], other_lines: list[str]) -> list[_Hunk]:
    matcher = difflib.SequenceMatcher(None, base_lines, other_lines, autojunk=False)
    return [(i1, i2, other_lines[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


def _hunk_precedes(first: _Hunk, second: _Hunk) -> bool:
    f1, f2, _ = first
    s1, s2, _ = second
    if f1 == f2 and s1 == s2:
        return f1 < s1
    return f2 <= s1


def _three_way_merge(base: str, target: str, candidate: str) -> str | None:
    """Merge ``base -> target`` and ``base -> candidate`` line-by-line, returning ``None`` on conflict."""
    if target == base:
        return candidate
    if candidate in (base, target):
        return target

    base_lines = base.splitlines(keepends=True)
    target_hunks = _change_hunks(base_lines, target.splitlines(keepends=True))
    cand_hunks = _change_hunks(base_lines, candidate.splitlines(keepends=True))

    out: list[str] = []
    pos = 0
    t_idx = 0
    c_idx = 0

    while t_idx < len(target_hunks) and c_idx < len(cand_hunks):
        t_hunk = target_hunks[t_idx]
        c_hunk = cand_hunks[c_idx]
        if t_hunk == c_hunk:
            out.extend(base_lines[pos : t_hunk[0]])
            out.extend(t_hunk[2])
            pos = t_hunk[1]
            t_idx += 1
            c_idx += 1
        elif _hunk_precedes(t_hunk, c_hunk):
            out.extend(base_lines[pos : t_hunk[0]])
            out.extend(t_hunk[2])
            pos = t_hunk[1]
            t_idx += 1
        elif _hunk_precedes(c_hunk, t_hunk):
            out.extend(base_lines[pos : c_hunk[0]])
            out.extend(c_hunk[2])
            pos = c_hunk[1]
            c_idx += 1
        else:
            return None

    for start, end, replacement in target_hunks[t_idx:] + cand_hunks[c_idx:]:
        out.extend(base_lines[pos:start])
        out.extend(replacement)
        pos = end

    out.extend(base_lines[pos:])
    return "".join(out)


def _merge_overlay_file(base_text: str | None, target_text: str | None, cand_text: str) -> tuple[bool, str | None]:
    """Merge a candidate file onto a replay target.

    Returns ``(ok, merged_content)`` where ``merged_content is None`` with ``ok=True``
    means the candidate did not modify the file relative to ``base_text``.
    """
    if base_text is None:
        if target_text is None or target_text == cand_text:
            return True, cand_text
        return False, None
    if cand_text == base_text:
        return True, None
    if target_text is None:
        return False, None
    merged = _three_way_merge(base_text, target_text, cand_text)
    if merged is None:
        return False, None
    return True, merged


def _normalize_workflow_name(workflow: str, default_workflow: str) -> str:
    if not workflow:
        return default_workflow
    return PurePosixPath(workflow).name or default_workflow


def _workflow_for_hypothesis(hypothesis: Hypothesis, default_workflow: str) -> str:
    """Resolve the workflow filename for ``hypothesis``, falling back to ``default_workflow``."""
    return _normalize_workflow_name(hypothesis.workflow_path, default_workflow)


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
        f"Implement hypothesis {hypothesis.id} ({hypothesis.skill}) for {repo} atop resolved cycle commit "
        f"{base_sha} (distinct from the hypothesis measurement commit) and report a single candidate with "
        f"base_sha={base_sha}.\n"
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


def _escape_md_cell(value: object) -> str:
    return " ".join(str(value).splitlines()).replace("|", r"\|")


def _format_bug_outcome_row(outcome: dict[str, Any]) -> str:
    kind = _escape_md_cell(outcome.get("kind", ""))
    ref = _escape_md_cell(outcome.get("ref", ""))
    baseline_failed = bool(outcome.get("baseline_failed", False))
    baseline_label = "Yes" if baseline_failed else "No"
    if bool(outcome.get("inconclusive", False)):
        caught_label = "Inconclusive (overlay conflict)"
    elif not baseline_failed:
        caught_label = "Excluded (dud)"
    elif bool(outcome.get("caught", False)):
        caught_label = "Yes"
    else:
        caught_label = "No"
    run_url = str(outcome.get("run_url") or "")
    run_cell = f"[View run]({run_url})" if run_url else "-"
    return f"| {kind} | `{ref}` | {baseline_label} | {caught_label} | {run_cell} |"


def _format_bug_replay_table(bug_outcomes: tuple[dict[str, Any], ...]) -> str:
    if not bug_outcomes:
        return "_No bug replay outcomes recorded._"
    lines = [
        "| Kind | Ref | Baseline Failed | Caught | Run |",
        "| --- | --- | --- | --- | --- |",
        *(_format_bug_outcome_row(outcome) for outcome in bug_outcomes),
    ]
    return "\n".join(lines)


def format_pr_title(hypothesis: Hypothesis, candidate: Candidate, verdict: Verdict) -> str:
    """Build a concise pull request title for a kept optimization candidate."""
    raw_summary = candidate.summary.strip() or hypothesis.description.strip()
    headline = raw_summary.splitlines()[0].strip() if raw_summary else hypothesis.skill
    return f"perf(ci): [{hypothesis.skill}] {headline} ({verdict.speedup:.2f}x faster)"


def format_pr_body(
    hypothesis: Hypothesis,
    candidate: Candidate,
    evidence: Evidence,
    verdict: Verdict,
) -> str:
    """Build a Markdown pull request body summarizing verdict metrics and bug-replay outcomes."""
    p_value_str = f"{verdict.p_value:.4f}" if verdict.p_value is not None else "N/A"
    sla_status = "Met" if verdict.sla_met else "Not met"
    replay_table = _format_bug_replay_table(evidence.bug_outcomes)
    return "\n".join(
        [
            "## Pit Crew CI Optimization",
            "",
            f"- **Hypothesis:** `{hypothesis.skill}` — {hypothesis.description}",
            f"- **Candidate:** `{candidate.id}` (`{candidate.branch}`)",
            f"- **Summary:** {candidate.summary}",
            f"- **Verdict:** {verdict.reason}",
            "",
            "### Verdict Summary",
            "",
            "| Metric | Value |",
            "| --- | --- |",
            f"| Speedup | {verdict.speedup:.2f}x |",
            f"| p-value | {p_value_str} |",
            f"| Median duration | {verdict.median_seconds:.1f}s |",
            f"| p95 duration | {verdict.p95_seconds:.1f}s |",
            f"| SLA status | {sla_status} |",
            f"| Bugs caught | {evidence.bugs_caught}/{evidence.bugs_total} |",
            "",
            "### Bug Replay Summary",
            "",
            replay_table,
            "",
        ]
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
    base_sha: str
    candidate: Candidate


@dataclass(frozen=True)
class _BranchReplaySpec:
    """Parameters for creating, committing to, running, and deleting an ephemeral replay branch."""

    branch: str
    base_sha: str
    files: dict[str, str]
    message: str


@dataclass(frozen=True)
class _BugReplayResult:
    """Outcome of replaying a single ``BugCase``."""

    counted: bool
    caught: bool
    outcome: dict[str, Any]
    run_urls: tuple[str, ...]


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
        self._declined_hypothesis_ids: set[str] = set()

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
        attempted_ids = {cand.hypothesis_id for cand in self.ledger.all(Candidate)} | self._declined_hypothesis_ids
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

        Creates a temporary named branch pinned to ``base_sha`` (and a temporary branch
        for ``candidate.head_sha`` if ``candidate.branch`` is empty) so GitHub Actions
        ``workflow_dispatch`` always receives a named ref rather than a raw commit SHA.
        Alternates baseline and candidate workflow dispatches for ``config.timing_pairs``
        pairs, including cold Run #1 to cancel runner-load drift, and cleans up temporary
        refs in ``finally``.

        Then replays each ``BugCase`` in ``replay_plan.cases`` on temporary branches that
        are always deleted in ``finally``:

        - ``kind == "commit"`` applies a 3-way merge of ``candidate.files`` onto
          ``bug_case.commit_sha`` and marks the bug caught iff CI concludes ``"failure"``.
        - ``kind == "mutation"`` rejects any mutation touching ``.github/``, first verifies
          anti-gaming baseline failure onto ``base_sha``, excludes dud mutations, and
          applies a 3-way merge of ``candidate.files`` onto ``bug_case.mutated_files``
          before running the candidate replay. Overlay conflicts are recorded as
          inconclusive rather than scoring a replay that erased the bug.
        """
        repo = self._resolve_repo(candidate)
        workflow_file = self._resolve_workflow(candidate, workflow)
        if not candidate.branch and not candidate.head_sha:
            msg = f"Candidate {candidate.id!r} has neither branch nor head_sha"
            raise ValueError(msg)

        baseline_seconds, candidate_seconds, timing_urls = self._collect_interleaved_timings(
            repo=repo,
            workflow=workflow_file,
            base_sha=base_sha,
            candidate=candidate,
        )
        target = _ReplayTarget(repo=repo, workflow=workflow_file, base_sha=base_sha, candidate=candidate)
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

    def _select_pending_candidate(self, repo: str) -> Candidate | None:
        repo_key = normalize_repo(repo)
        hyp_ids = {hyp.id for hyp in self.ledger.hypotheses_for_repo(repo)}
        rated_ids = {verdict.candidate_id for verdict in self.ledger.all(Verdict)}
        for cand in self.ledger.all(Candidate):
            if cand.id not in rated_ids and (cand.hypothesis_id in hyp_ids or normalize_repo(cand.repo) == repo_key):
                return cand
        return None

    def _existing_replay_plan(self, candidate_id: str) -> ReplayPlan | None:
        plans = [plan for plan in self.ledger.all(ReplayPlan) if plan.candidate_id == candidate_id]
        return plans[-1] if plans else None

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
            return _normalize_workflow_name(workflow, self.config.workflow_file)
        hypothesis = self._find_hypothesis(candidate.hypothesis_id)
        if hypothesis is not None:
            return _workflow_for_hypothesis(hypothesis, self.config.workflow_file)
        return self.config.workflow_file

    def _verdict_matches_target_workflow(self, verdict: Verdict) -> bool:
        target_workflow = _normalize_workflow_name(self.config.workflow_file, self.config.workflow_file)
        try:
            cand = self.ledger.get(Candidate, verdict.candidate_id)
        except KeyError:
            return True
        measured = self._resolve_workflow(cand, workflow=None)
        return measured == target_workflow

    def _publish_candidate(self, repo: str, base_sha: str, candidate: Candidate) -> Candidate:
        if not candidate.head_sha and candidate.files:
            if not candidate.branch:
                msg = f"Candidate {candidate.id!r} cannot be published without a branch name"
                raise ValueError(msg)
            self.github.create_branch(repo, candidate.branch, base_sha)
            try:
                head_sha = self.github.commit_files(
                    repo,
                    candidate.branch,
                    base_sha,
                    dict(candidate.files),
                    f"Pit Crew candidate {candidate.id}: {candidate.summary}",
                )
            except Exception:
                self.github.delete_branch(repo, candidate.branch)
                raise
            published = replace(candidate, repo=repo, base_sha=base_sha, head_sha=head_sha)
            self.ledger.add(published)
            return published

        if candidate.base_sha != base_sha or not candidate.repo:
            updated = replace(candidate, repo=candidate.repo or repo, base_sha=base_sha)
            self.ledger.add(updated)
            return updated
        return candidate

    def _dispatch_and_poll(self, repo: str, workflow: str, ref: str) -> WorkflowRunResult:
        run_id = self.github.dispatch_workflow(repo, workflow, ref)
        return self.poll_workflow_run(repo, run_id)

    def _collect_interleaved_timings(
        self,
        *,
        repo: str,
        workflow: str,
        base_sha: str,
        candidate: Candidate,
    ) -> tuple[tuple[float, ...], tuple[float, ...], tuple[str, ...]]:
        if self.config.timing_pairs <= 0:
            return (), (), ()

        run_urls: list[str] = []
        with ExitStack() as stack:
            baseline_branch = f"pc/baseline-{candidate.id}"
            self.github.create_branch(repo, baseline_branch, base_sha)
            stack.callback(self.github.delete_branch, repo, baseline_branch)

            candidate_ref = candidate.branch
            if not candidate_ref:
                candidate_ref = f"pc/candidate-{candidate.id}"
                self.github.create_branch(repo, candidate_ref, candidate.head_sha)
                stack.callback(self.github.delete_branch, repo, candidate_ref)

            baseline = _TimingSide(ref=baseline_branch, durations=[])
            cand_side = _TimingSide(ref=candidate_ref, durations=[])

            for _ in range(self.config.timing_pairs):
                for side in (baseline, cand_side):
                    if side.active:
                        self._record_timing_run(repo, workflow, side, run_urls)
                if not baseline.active and not cand_side.active:
                    break

        return tuple(baseline.durations), tuple(cand_side.durations), tuple(run_urls)

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
            result = self._replay_bug_case(target, bug_case, idx)
            if result.counted:
                bugs_total += 1
                if result.caught:
                    bugs_caught += 1
            run_urls.extend(result.run_urls)
            bug_outcomes.append(result.outcome)

        return bugs_total, bugs_caught, tuple(run_urls), tuple(bug_outcomes)

    def _replay_bug_case(
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
    ) -> _BugReplayResult:
        if bug_case.kind == "commit":
            return self._replay_commit_case(target, bug_case, idx)
        if bug_case.kind == "mutation":
            return self._replay_mutation_case(target, bug_case, idx)
        msg = f"Unsupported bug case kind at index {idx}: {bug_case.kind!r}"
        raise ValueError(msg)

    def _run_ephemeral_branch(self, target: _ReplayTarget, spec: _BranchReplaySpec) -> WorkflowRunResult:
        self.github.create_branch(target.repo, spec.branch, spec.base_sha)
        try:
            self.github.commit_files(target.repo, spec.branch, spec.base_sha, spec.files, spec.message)
            return self._dispatch_and_poll(target.repo, target.workflow, spec.branch)
        finally:
            self.github.delete_branch(target.repo, spec.branch)

    def _build_commit_replay_files(
        self,
        target: _ReplayTarget,
        commit_sha: str,
    ) -> tuple[dict[str, str], list[str]]:
        candidate_base = target.candidate.base_sha or target.base_sha
        overlay: dict[str, str] = {}
        conflicts: list[str] = []
        for path, cand_text in sorted(target.candidate.files.items()):
            base_text = self.github.get_file_content(target.repo, path, candidate_base)
            target_text = self.github.get_file_content(target.repo, path, commit_sha)
            ok, merged = _merge_overlay_file(base_text, target_text, cand_text)
            if not ok:
                conflicts.append(path)
            elif merged is not None:
                overlay[path] = merged
        return overlay, conflicts

    def _build_mutation_replay_files(
        self,
        target: _ReplayTarget,
        mutated_files: dict[str, str],
    ) -> tuple[dict[str, str], list[str]]:
        candidate_base = target.candidate.base_sha or target.base_sha
        overlay = dict(mutated_files)
        conflicts: list[str] = []
        for path, cand_text in sorted(target.candidate.files.items()):
            if path not in mutated_files:
                overlay[path] = cand_text
                continue
            base_text = self.github.get_file_content(target.repo, path, candidate_base)
            ok, merged = _merge_overlay_file(base_text, mutated_files[path], cand_text)
            if not ok:
                conflicts.append(path)
            elif merged is not None:
                overlay[path] = merged
        return overlay, conflicts

    def _replay_commit_case(
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
    ) -> _BugReplayResult:
        commit_sha = bug_case.commit_sha
        if not commit_sha:
            msg = f"Commit bug case at index {idx} is missing commit_sha"
            raise ValueError(msg)

        overlay_files, conflicts = self._build_commit_replay_files(target, commit_sha)
        if conflicts:
            outcome: dict[str, Any] = {
                "kind": "commit",
                "ref": commit_sha,
                "caught": False,
                "baseline_failed": True,
                "inconclusive": True,
                "conflict_paths": conflicts,
                "run_url": "",
            }
            return _BugReplayResult(counted=False, caught=False, outcome=outcome, run_urls=())

        run = self._run_ephemeral_branch(
            target,
            _BranchReplaySpec(
                branch=f"pc/replay-{target.candidate.id}-{idx}",
                base_sha=commit_sha,
                files=overlay_files,
                message=f"Pit Crew replay: overlay candidate {target.candidate.id} onto {commit_sha}",
            ),
        )
        caught = run.conclusion == "failure"
        outcome = {
            "kind": "commit",
            "ref": commit_sha,
            "caught": caught,
            "baseline_failed": True,
            "run_url": run.html_url,
        }
        run_urls = (run.html_url,) if run.html_url else ()
        return _BugReplayResult(counted=True, caught=caught, outcome=outcome, run_urls=run_urls)

    def _replay_mutation_case(
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
    ) -> _BugReplayResult:
        if not bug_case.mutated_files:
            msg = f"Mutation bug case at index {idx} is missing mutated_files"
            raise ValueError(msg)

        for path in sorted(bug_case.mutated_files):
            if not _is_allowed_mutation_path(path):
                msg = f"Disallowed mutation path at index {idx}: {path!r}"
                raise ValueError(msg)

        ref = bug_case.reason or ",".join(sorted(bug_case.mutated_files))
        baseline_run = self._run_ephemeral_branch(
            target,
            _BranchReplaySpec(
                branch=f"pc/replay-{target.candidate.id}-{idx}-base",
                base_sha=target.base_sha,
                files=dict(bug_case.mutated_files),
                message=f"Pit Crew mutation baseline replay {idx} for candidate {target.candidate.id}",
            ),
        )
        run_urls: list[str] = [baseline_run.html_url] if baseline_run.html_url else []
        baseline_failed = baseline_run.conclusion == "failure"
        if not baseline_failed:
            outcome: dict[str, Any] = {
                "kind": "mutation",
                "ref": ref,
                "caught": False,
                "baseline_failed": False,
                "run_url": baseline_run.html_url,
            }
            return _BugReplayResult(counted=False, caught=False, outcome=outcome, run_urls=tuple(run_urls))

        overlay_files, conflicts = self._build_mutation_replay_files(target, bug_case.mutated_files)
        if conflicts:
            outcome = {
                "kind": "mutation",
                "ref": ref,
                "caught": False,
                "baseline_failed": True,
                "inconclusive": True,
                "conflict_paths": conflicts,
                "run_url": baseline_run.html_url,
            }
            return _BugReplayResult(counted=False, caught=False, outcome=outcome, run_urls=tuple(run_urls))

        candidate_run = self._run_ephemeral_branch(
            target,
            _BranchReplaySpec(
                branch=f"pc/replay-{target.candidate.id}-{idx}-cand",
                base_sha=target.base_sha,
                files=overlay_files,
                message=f"Pit Crew mutation candidate replay {idx} for candidate {target.candidate.id}",
            ),
        )
        if candidate_run.html_url:
            run_urls.append(candidate_run.html_url)
        caught = candidate_run.conclusion == "failure"
        outcome = {
            "kind": "mutation",
            "ref": ref,
            "caught": caught,
            "baseline_failed": True,
            "run_url": candidate_run.html_url,
        }
        return _BugReplayResult(counted=True, caught=caught, outcome=outcome, run_urls=tuple(run_urls))

    def run_cycle(self, repo: str, base_sha: str | None = None) -> Verdict | None:
        """Run one optimization cycle for ``repo`` and return the resulting ``Verdict``, if any.

        Resumes any pending unrated ``Candidate`` first so incomplete Proof or evidence
        steps are not permanently skipped. Otherwise runs ``analysis -> optimizer``,
        publishes validated candidate branches atop the resolved cycle base, runs
        ``proof``, collects evidence, and persists the Rater ``Verdict``. When
        ``verdict.keep`` and ``config.auto_open_pr`` are both ``True``, opens a pull
        request for ``candidate.branch`` targeting ``config.base_branch``.
        """
        pending = self._select_pending_candidate(repo)
        if pending is not None:
            pending_base = base_sha or pending.base_sha or self.resolve_base_sha(repo)
            return self._evaluate_candidate(repo, pending_base, pending, self._find_hypothesis(pending.hypothesis_id))

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

        candidate, optimizer_completed = self._run_optimizer(repo, resolved_base_sha, hypothesis)
        if candidate is None:
            if optimizer_completed:
                self._declined_hypothesis_ids.add(hypothesis.id)
            return None

        return self._evaluate_candidate(repo, resolved_base_sha, candidate, hypothesis)

    def _evaluate_candidate(
        self,
        repo: str,
        resolved_base_sha: str,
        candidate: Candidate,
        hypothesis: Hypothesis | None,
    ) -> Verdict | None:
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

        candidate = self._publish_candidate(repo, resolved_base_sha, candidate)

        replay_plan = self._existing_replay_plan(candidate.id)
        if replay_plan is None:
            replay_plan, proof_completed = self._run_proof(repo, resolved_base_sha, candidate)
            if replay_plan is None:
                if proof_completed:
                    verdict = Verdict(
                        candidate_id=candidate.id,
                        keep=False,
                        speedup=0.0,
                        p_value=None,
                        reason=NO_REPLAY_PLAN_REASON,
                    )
                    self.ledger.add(verdict)
                    return verdict
                return None

        workflow = (
            _workflow_for_hypothesis(hypothesis, self.config.workflow_file)
            if hypothesis is not None
            else self.config.workflow_file
        )
        evidence = self.collect_evidence(candidate, replay_plan, resolved_base_sha, workflow=workflow)
        self.ledger.add(evidence)
        verdict = rate(evidence)
        self.ledger.add(verdict)
        if verdict.keep and self.config.auto_open_pr and hypothesis is not None:
            self.github.open_pull_request(
                repo,
                title=format_pr_title(hypothesis, candidate, verdict),
                body=format_pr_body(hypothesis, candidate, evidence, verdict),
                head=candidate.branch,
                base=self.config.base_branch,
            )
        return verdict

    def _run_optimizer(self, repo: str, base_sha: str, hypothesis: Hypothesis) -> tuple[Candidate | None, bool]:
        existing_ids = {cand.id for cand in self.ledger.all(Candidate)}
        task = _build_optimizer_task(
            repo=repo,
            base_sha=base_sha,
            hypothesis=hypothesis,
            prior_attempts=self.ledger.prior_attempts(repo),
        )
        result = self.lane_runner("optimizer", task, self.ledger, repo=repo)
        new_candidates = [
            cand
            for cand in self.ledger.all(Candidate)
            if cand.id not in existing_ids and cand.hypothesis_id == hypothesis.id
        ]
        return (new_candidates[-1] if new_candidates else None), _is_completed_lane_result(result)

    def _run_proof(self, repo: str, base_sha: str, candidate: Candidate) -> tuple[ReplayPlan | None, bool]:
        existing_ids = {plan.id for plan in self.ledger.all(ReplayPlan)}
        task = _build_proof_task(repo=repo, base_sha=base_sha, candidate=candidate)
        result = self.lane_runner("proof", task, self.ledger, repo=repo)
        new_plans = [
            plan
            for plan in self.ledger.all(ReplayPlan)
            if plan.id not in existing_ids and plan.candidate_id == candidate.id
        ]
        return (new_plans[-1] if new_plans else None), _is_completed_lane_result(result)

    def run_until_sla(self, repo: str) -> list[Verdict]:
        """Run compounding optimization cycles for ``repo`` until the CI SLA is met or cycles are exhausted.

        Repeatedly invokes ``run_cycle(repo)`` up to ``config.max_compounding_cycles`` iterations,
        collecting each non-``None`` ``Verdict``. Each subsequent cycle automatically advances
        ``base_sha`` to ``ledger.latest_kept_candidate(repo).head_sha`` after a kept candidate,
        while retaining the previous ``base_sha`` after a rejected candidate. Continues past
        hypotheses definitively declined by Optimizer while remaining hypotheses exist, and
        stops early only when a kept verdict for ``config.workflow_file`` satisfies the CI SLA.
        """
        verdicts: list[Verdict] = []
        for _ in range(self.config.max_compounding_cycles):
            declined_before = len(self._declined_hypothesis_ids)
            verdict = self.run_cycle(repo)
            if verdict is None:
                if (
                    len(self._declined_hypothesis_ids) > declined_before
                    and self.select_next_hypothesis(repo) is not None
                ):
                    continue
                break
            verdicts.append(verdict)
            if verdict.keep and verdict.sla_met and self._verdict_matches_target_workflow(verdict):
                break
        return verdicts
