"""Orchestrates Pit Crew lane handoffs, interleaved timing runs, and safety guards.

Drives a single optimization cycle through ``analysis -> optimizer -> proof`` while
keeping all ledger state, workflow timing collection, Rater verdicts, and trust
boundaries on the host. Never imports ``google`` or any lane module directly; lane
execution goes through the injected ``LaneRunner`` or ``ProofBridge``.
"""

import contextvars
import difflib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, dataclass, replace
from importlib import import_module
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pit_crew import workspace as host_workspace
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
from pit_crew.lanes.optimizer.tools import check_candidate_files
from pit_crew.ledger import Ledger, normalize_repo
from pit_crew.proof_bridge import (
    MultimodalReplayVerifier,
    ProofBridgeError,
    ProofPRConfig,
    ReplayFailureOutputClient,
    ReplayRunPair,
    ReplayVerification,
    build_replay_overlay,
    resolve_workflow_check_aliases,
)
from pit_crew.rater import rate

DISALLOWED_PATH_REASON = "disallowed path modified"
CHECKS_LESS_REASON = "candidate checks less than its base"
NO_REPLAY_PLAN_REASON = "no replay plan produced, so safety is unproven"

CycleStage = Literal["analysis", "optimizer", "proof", "timing", "replay", "rating"]
ProgressReporter = Callable[[CycleStage, int, int], None]

_Hunk = tuple[int, int, list[str]]
_MISSING_RUNNER_MSG = str(ProofBridgeError.missing_runner())
_MISSING_PR_CLIENT_MSG = str(ProofBridgeError.missing_pr_client())
_WORKFLOW_PREFERENCES = ("ci", "test", "tests", "build", "check")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TargetContext:
    """Resolved target repository, default branch, workflow file, and upstream history source."""

    ledger_repo: str
    work_repo: str
    history_repo: str | None
    base_branch: str
    workflow_file: str


def _pick_workflow_file(configured: str, workflows: list[str]) -> str:
    """Choose the best active workflow file, keeping ``configured`` whenever it is present."""
    if not workflows or configured in workflows:
        return configured
    for keyword in _WORKFLOW_PREFERENCES:
        for name in workflows:
            stem = PurePosixPath(name).stem.lower()
            if stem == keyword or keyword in stem:
                return name
    return workflows[0]


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
    max_parallel_runs: int = 1
    optimizer_fanout: int = 1  # Optimizer lanes run at once on the top hypotheses; the first candidate wins


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


class AskMultimodal(Protocol):
    """Callable boundary for multimodal failure verification (e.g. ``managed_agent.ask_multimodal``)."""

    def __call__(
        self,
        model: str,
        parts: Sequence[tuple[str, str | bytes]],
        schema: Mapping[str, object],
    ) -> str:
        """Send multimodal prompt parts to ``model`` and return JSON constrained to ``schema``."""
        ...


@runtime_checkable
class ReplayVerifier(Protocol):
    """Verifies paired baseline and candidate replay runs and returns Proof outcomes."""

    def verify_replay_runs(
        self,
        repo: str,
        runs: Iterable[ReplayRunPair],
    ) -> ReplayVerification:
        """Verify replay pairs and return ``ReplayVerification`` populated from ``tally()`` and ``as_outcome()``."""
        ...


def _default_ask_multimodal(
    model: str,
    parts: Sequence[tuple[str, str | bytes]],
    schema: Mapping[str, object],
) -> str:
    """Lazily resolve and invoke ``pit_crew.managed_agent.ask_multimodal``."""
    module = import_module("pit_crew.managed_agent")
    ask = cast("AskMultimodal", module.ask_multimodal)
    return ask(model, parts, schema)


def build_multimodal_verifier(
    github: ReplayFailureOutputClient,
    *,
    ask: AskMultimodal | None = None,
    media_dir: Path | str | None = None,
    on_media_saved: Callable[[Path], None] | None = None,
) -> MultimodalReplayVerifier:
    """Build a replay verifier wired to ``verify_replays``, ``tally``, and ``Replay.as_outcome``.

    Defaults ``ask`` to ``pit_crew.managed_agent.ask_multimodal`` (resolved lazily so
    ``dispatcher`` never statically imports ``google-genai``).
    """
    return MultimodalReplayVerifier(
        github,
        ask=ask if ask is not None else _default_ask_multimodal,
        media_dir=media_dir,
        on_media_saved=on_media_saved,
    )


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


def _validate_bug_case(bug_case: BugCase, idx: int) -> None:
    """Validate a single ``BugCase`` up front before creating any replay branches."""
    if bug_case.kind == "commit":
        if not bug_case.commit_sha:
            msg = f"Commit bug case at index {idx} is missing commit_sha"
            raise ValueError(msg)
        return
    if bug_case.kind == "mutation":
        if not bug_case.mutated_files:
            msg = f"Mutation bug case at index {idx} is missing mutated_files"
            raise ValueError(msg)
        for path in sorted(bug_case.mutated_files):
            if not _is_allowed_mutation_path(path):
                msg = f"Disallowed mutation path at index {idx}: {path!r}"
                raise ValueError(msg)
        return
    msg = f"Unsupported bug case kind at index {idx}: {bug_case.kind!r}"
    raise ValueError(msg)


def _check_label_for_case(bug_case: BugCase, workflow: str) -> str:
    if bug_case.expected_checks:
        return bug_case.expected_checks[0]
    return workflow


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
    history_repo: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "repo": repo,
        "base_sha": base_sha,
        "workflow_file": workflow_file,
        "prior_attempts": prior_attempts,
    }
    extra_note = ""
    if history_repo and normalize_repo(history_repo) != normalize_repo(repo):
        payload["history_repo"] = history_repo
        extra_note = (
            f" If {repo} has no completed CI runs yet, profile CI run history from upstream {history_repo}, "
            f"and always pass repo={repo!r} when calling record_hypothesis."
        )
    return (
        f"Profile CI workflows for {repo} at base commit {base_sha} and record prioritized hypotheses."
        f"{extra_note}\n"
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


def _bug_caught_label(outcome: dict[str, Any], *, baseline_failed: bool) -> str:
    if bool(outcome.get("inconclusive", False)):
        return "Inconclusive (overlay conflict)"
    status = str(outcome.get("status") or "")
    if not baseline_failed or status == "not a bug":
        return "Excluded (dud)"
    if status in {"different failure", "unverified"}:
        return f"No ({status})"
    if bool(outcome.get("caught", False)) or status == "caught":
        return "Yes"
    return "No"


def _format_bug_outcome_row(outcome: dict[str, Any]) -> str:
    kind = _escape_md_cell(outcome.get("kind", ""))
    ref = _escape_md_cell(outcome.get("ref") or outcome.get("sha", ""))
    baseline_failed = bool(outcome.get("baseline_failed", False))
    baseline_label = "Yes" if baseline_failed else "No"
    caught_label = _bug_caught_label(outcome, baseline_failed=baseline_failed)
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
    """Parameters for creating, optionally committing to, running, and deleting an ephemeral replay branch."""

    branch: str
    base_sha: str
    files: dict[str, str]
    message: str
    commit: bool = True


@dataclass(frozen=True)
class _ReplayPairMeta:
    """Dispatcher metadata paired with a ``ReplayRunPair`` for ``Evidence.bug_outcomes``."""

    kind: str
    ref: str
    baseline_run_url: str
    candidate_run_url: str


@dataclass(frozen=True)
class _BugReplayResult:
    """Outcome of replaying a single ``BugCase``."""

    counted: bool
    caught: bool
    outcome: dict[str, Any]
    run_urls: tuple[str, ...]
    pair: ReplayRunPair | None = None
    pair_meta: _ReplayPairMeta | None = None


def _primary_check_name(bug_case: BugCase, workflow: str) -> str:
    return bug_case.expected_checks[0] if bug_case.expected_checks else workflow


def _inconclusive_replay_result(
    kind: str,
    ref: str,
    conflicts: list[str],
    *,
    run_url: str = "",
    run_urls: tuple[str, ...] = (),
) -> _BugReplayResult:
    outcome: dict[str, Any] = {
        "kind": kind,
        "ref": ref,
        "caught": False,
        "baseline_failed": True,
        "inconclusive": True,
        "conflict_paths": conflicts,
        "run_url": run_url,
    }
    return _BugReplayResult(counted=False, caught=False, outcome=outcome, run_urls=run_urls)


def _fold_parallel_timings(
    runs: list[tuple[str, WorkflowRunResult]],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[str, ...]]:
    baseline_durations: list[float] = []
    cand_durations: list[float] = []
    run_urls: list[str] = []
    base_active = True
    cand_active = True
    for side_name, run in runs:
        if run.html_url:
            run_urls.append(run.html_url)
        if side_name == "base":
            if base_active and run.conclusion == "success":
                baseline_durations.append(run.duration_seconds)
            elif run.conclusion != "success":
                base_active = False
        elif cand_active and run.conclusion == "success":
            cand_durations.append(run.duration_seconds)
        elif run.conclusion != "success":
            cand_active = False
    return tuple(baseline_durations), tuple(cand_durations), tuple(run_urls)


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
        replay_verifier: ReplayVerifier | None = None,
        record_replays: bool | None = None,
        run_tracker: Any | None = None,  # noqa: ANN401
        use_host_workspace: bool = False,
        proof_bridge: Any | None = None,  # noqa: ANN401
    ) -> None:
        """Initialize the Dispatcher with a ledger, GitHub client, lane runner, config, and runtime hooks."""
        self.ledger = ledger
        self.github = github
        self.lane_runner: LaneRunner = lane_runner if lane_runner is not None else _default_lane_runner
        self.config = config if config is not None else DispatcherConfig()
        self._sleep = sleep
        self._monotonic = monotonic
        self._replay_verifier = replay_verifier
        self._record_replays = (
            record_replays if record_replays is not None else (replay_verifier is not None or proof_bridge is not None)
        )
        self._run_tracker = run_tracker
        self._use_host_workspace = use_host_workspace
        self._proof_bridge = proof_bridge
        self._declined_hypothesis_ids: set[str] = set()
        self._inflight_hypothesis_ids: set[str] = set()
        self._target_cache: dict[str, _TargetContext] = {}
        self._cycle_local = threading.local()

    def _resolve_target_context(self, repo: str) -> _TargetContext:
        key = normalize_repo(repo)
        cached = self._target_cache.get(key)
        if cached is not None:
            return cached

        work_repo = repo
        history_repo: str | None = None
        base_branch = self.config.base_branch

        get_meta = getattr(self.github, "get_repo_metadata", None)
        if callable(get_meta):
            meta = get_meta(repo)
            default_branch = getattr(meta, "default_branch", "")
            if isinstance(default_branch, str) and default_branch and base_branch == "main":
                base_branch = default_branch
            can_push = bool(getattr(meta, "can_push", True))
            parent_full_name = getattr(meta, "parent_full_name", None)
            if not can_push:
                ensure_fork = getattr(self.github, "ensure_writable_fork", None)
                if callable(ensure_fork):
                    work_repo = str(ensure_fork(repo))
                    history_repo = repo
            elif isinstance(parent_full_name, str) and parent_full_name:
                history_repo = parent_full_name

        workflow_file = self.config.workflow_file
        list_workflows = getattr(self.github, "list_workflow_files", None)
        if callable(list_workflows):
            workflows = [str(w) for w in list_workflows(history_repo or work_repo)]
            workflow_file = _pick_workflow_file(workflow_file, workflows)

        ctx = _TargetContext(
            ledger_repo=repo,
            work_repo=work_repo,
            history_repo=history_repo,
            base_branch=base_branch,
            workflow_file=workflow_file,
        )
        self._target_cache[key] = ctx
        if work_repo != repo or base_branch != self.config.base_branch or workflow_file != self.config.workflow_file:
            logger.info(
                "Resolved target %s -> work_repo=%s history_repo=%s base_branch=%s workflow=%s",
                repo,
                work_repo,
                history_repo,
                base_branch,
                workflow_file,
            )
        return ctx

    def _work_repo_for(self, repo: str) -> str:
        return self._resolve_target_context(repo).work_repo

    def _base_branch_for(self, repo: str) -> str:
        return self._resolve_target_context(repo).base_branch

    def _workflow_default_for(self, repo: str) -> str:
        return self._resolve_target_context(repo).workflow_file

    def _report_progress(self, stage: CycleStage, completed_runs: int = 0, total_runs: int = 0) -> None:
        reporter: ProgressReporter | None = getattr(self._cycle_local, "on_progress", None)
        if reporter is not None:
            reporter(stage, completed_runs, total_runs)

    def _set_verified_replays(
        self,
        replays: tuple[Any, ...],
        preview_files: tuple[tuple[str, bytes], ...] = (),
        video_paths: tuple[str, ...] = (),
    ) -> None:
        self._cycle_local.verified_replays = replays
        self._cycle_local.preview_files = preview_files
        self._cycle_local.saved_video_paths = video_paths

    def _get_verified_replays(self) -> tuple[Any, ...]:
        replays: tuple[Any, ...] = getattr(self._cycle_local, "verified_replays", ())
        return replays

    def _get_preview_files(self) -> tuple[tuple[str, bytes], ...]:
        previews: tuple[tuple[str, bytes], ...] = getattr(self._cycle_local, "preview_files", ())
        return previews

    @property
    def last_replay_videos(self) -> tuple[str, ...]:
        """Return persisted local MP4 replay file paths from the most recent cycle."""
        paths: tuple[str, ...] = getattr(self._cycle_local, "saved_video_paths", ())
        return paths

    @property
    def last_opened_pr_url(self) -> str | None:
        """Return the URL of the pull request opened in the most recent cycle, if any."""
        url: str | None = getattr(self._cycle_local, "last_pr_url", None)
        return url

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
        return self.github.get_default_branch_sha(self._work_repo_for(repo))

    def select_next_hypothesis(self, repo: str) -> Hypothesis | None:
        """Return the highest-impact unattempted ``Hypothesis`` for ``repo``, or ``None``."""
        top = self._next_hypotheses(repo, 1)
        return top[0] if top else None

    def _next_hypotheses(self, repo: str, limit: int) -> list[Hypothesis]:
        """Return up to ``limit`` unattempted, not in-flight hypotheses for ``repo``, highest impact first."""
        attempted_ids = (
            {cand.hypothesis_id for cand in self.ledger.all(Candidate)}
            | self._declined_hypothesis_ids
            | self._inflight_hypothesis_ids
        )
        unattempted = [hyp for hyp in self.ledger.hypotheses_for_repo(repo) if hyp.id not in attempted_ids]
        return sorted(unattempted, key=lambda hyp: hyp.expected_savings_seconds, reverse=True)[: max(1, limit)]

    def validate_candidate_paths(self, candidate: Candidate) -> tuple[str, ...]:
        """Return any disallowed file paths touched by ``candidate``."""
        paths = dict.fromkeys((*candidate.changed_files, *candidate.files))
        return tuple(path for path in paths if not is_allowed_candidate_path(path))

    def guard_candidate(self, repo: str, base_sha: str, candidate: Candidate) -> list[str]:
        """Run the Optimizer's never-check-less guard against the real files at ``base_sha``.

        The agent reports `original_files` itself; re-checking against GitHub's copy means a wrong or
        invented "before" text cannot get a weakening change published.
        """
        work_repo = self._work_repo_for(repo)
        fetched = {path: self.github.get_file_content(work_repo, path, base_sha) for path in candidate.files}
        base = {path: text for path, text in fetched.items() if text is not None}
        return check_candidate_files(candidate.files, base)

    def poll_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
        """Poll ``github.get_workflow_run`` until ``run_id`` completes or times out."""
        if self._run_tracker is not None:
            self._run_tracker.register(run_id)
        try:
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
                if self._run_tracker is not None:
                    wait_for = min(self.config.poll_interval_seconds, self.config.run_timeout_seconds - elapsed)
                    self._run_tracker.wait_sync(run_id, timeout=wait_for)
                else:
                    self._sleep(self.config.poll_interval_seconds)
        finally:
            if self._run_tracker is not None:
                self._run_tracker.discard(run_id)

    def collect_evidence(
        self,
        candidate: Candidate,
        replay_plan: ReplayPlan,
        base_sha: str,
        *,
        workflow: str | None = None,
    ) -> Evidence:
        """Collect interleaved CI timing and bug-replay evidence for ``candidate`` against ``base_sha``."""
        repo = self._resolve_repo(candidate)
        work_repo = self._work_repo_for(repo)
        workflow_file = self._resolve_workflow(candidate, workflow, repo=repo)
        if not candidate.branch and not candidate.head_sha:
            msg = f"Candidate {candidate.id!r} has neither branch nor head_sha"
            raise ValueError(msg)

        baseline_seconds, candidate_seconds, timing_urls = self._collect_interleaved_timings(
            repo=work_repo,
            workflow=workflow_file,
            base_sha=base_sha,
            candidate=candidate,
        )
        target = _ReplayTarget(repo=work_repo, workflow=workflow_file, base_sha=base_sha, candidate=candidate)
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
        # A candidate built before the latest KEEP lacks that change, so it is never evaluated on the new base.
        kept = self.ledger.latest_kept_candidate(repo)
        current_base = kept.head_sha if kept is not None else ""
        for cand in self.ledger.all(Candidate):
            stale = bool(current_base and cand.base_sha and cand.base_sha != current_base)
            in_repo = cand.hypothesis_id in hyp_ids or normalize_repo(cand.repo) == repo_key
            if cand.id not in rated_ids and in_repo and not stale:
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

    def _resolve_workflow(self, candidate: Candidate, workflow: str | None, *, repo: str | None = None) -> str:
        default_wf = self._workflow_default_for(repo) if repo else self.config.workflow_file
        if workflow:
            return _normalize_workflow_name(workflow, default_wf)
        hypothesis = self._find_hypothesis(candidate.hypothesis_id)
        if hypothesis is not None:
            return _workflow_for_hypothesis(hypothesis, default_wf)
        return default_wf

    def _verdict_matches_target_workflow(self, verdict: Verdict) -> bool:
        try:
            cand = self.ledger.get(Candidate, verdict.candidate_id)
        except KeyError:
            return True
        repo = cand.repo or (hyp.repo if (hyp := self._find_hypothesis(cand.hypothesis_id)) is not None else "")
        default_wf = self._workflow_default_for(repo) if repo else self.config.workflow_file
        target_workflow = _normalize_workflow_name(default_wf, default_wf)
        measured = self._resolve_workflow(cand, workflow=None, repo=repo or None)
        return measured == target_workflow

    def _publish_candidate(self, repo: str, base_sha: str, candidate: Candidate) -> Candidate:
        work_repo = self._work_repo_for(repo)
        if not candidate.head_sha and candidate.files:
            if not candidate.branch:
                msg = f"Candidate {candidate.id!r} cannot be published without a branch name"
                raise ValueError(msg)
            self.github.create_branch(work_repo, candidate.branch, base_sha)
            try:
                head_sha = self.github.commit_files(
                    work_repo,
                    candidate.branch,
                    base_sha,
                    dict(candidate.files),
                    f"Pit Crew candidate {candidate.id}: {candidate.summary}",
                )
            except Exception:
                self.github.delete_branch(work_repo, candidate.branch)
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
            self._report_progress("timing", 0, 0)
            return (), (), ()

        if self.config.max_parallel_runs > 1:
            return self._collect_parallel_timings(repo=repo, workflow=workflow, base_sha=base_sha, candidate=candidate)

        total_runs = self.config.timing_pairs * 2
        completed_runs = 0
        self._report_progress("timing", 0, total_runs)
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
                        completed_runs += 1
                        self._report_progress("timing", completed_runs, total_runs)
                if not baseline.active and not cand_side.active:
                    break

        return tuple(baseline.durations), tuple(cand_side.durations), tuple(run_urls)

    def _collect_parallel_timings(
        self,
        *,
        repo: str,
        workflow: str,
        base_sha: str,
        candidate: Candidate,
    ) -> tuple[tuple[float, ...], tuple[float, ...], tuple[str, ...]]:
        total_runs = self.config.timing_pairs * 2
        completed_runs = 0
        lock = threading.Lock()
        self._report_progress("timing", 0, total_runs)

        def run_slot(side_name: str, ref: str) -> tuple[str, WorkflowRunResult]:
            nonlocal completed_runs
            run = self._dispatch_and_poll(repo, workflow, ref)
            with lock:
                completed_runs += 1
                done = completed_runs
            self._report_progress("timing", done, total_runs)
            return side_name, run

        with ExitStack() as stack:
            slots: list[tuple[str, str]] = []
            for idx in range(self.config.timing_pairs):
                base_ref = f"pc/base-{candidate.id}-{idx}"
                self.github.create_branch(repo, base_ref, base_sha)
                stack.callback(self.github.delete_branch, repo, base_ref)
                if candidate.head_sha:
                    cand_ref = f"pc/cand-{candidate.id}-{idx}"
                    self.github.create_branch(repo, cand_ref, candidate.head_sha)
                    stack.callback(self.github.delete_branch, repo, cand_ref)
                else:
                    cand_ref = candidate.branch
                slots.append(("base", base_ref))
                slots.append(("cand", cand_ref))

            with ThreadPoolExecutor(max_workers=self.config.max_parallel_runs) as pool:
                futures = [pool.submit(run_slot, side_name, ref) for side_name, ref in slots]
                ordered_runs = [future.result() for future in futures]

        return _fold_parallel_timings(ordered_runs)

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
        cases = replay_plan.cases
        total_cases = len(cases)
        self._report_progress("replay", 0, total_cases)
        for idx, bug_case in enumerate(cases):
            _validate_bug_case(bug_case, idx)

        completed_cases = 0
        lock = threading.Lock()
        sem = threading.BoundedSemaphore(max(1, self.config.max_parallel_runs))

        def run_case(idx: int, bug_case: BugCase) -> _BugReplayResult:
            nonlocal completed_cases
            res = self._replay_bug_case(target, bug_case, idx, sem=sem)
            with lock:
                completed_cases += 1
                done = completed_cases
            self._report_progress("replay", done, total_cases)
            return res

        if self.config.max_parallel_runs > 1 and cases:
            with ThreadPoolExecutor(max_workers=len(cases)) as pool:
                futures = [pool.submit(run_case, idx, bug_case) for idx, bug_case in enumerate(cases)]
                results = [future.result() for future in futures]
        else:
            results = [run_case(idx, bug_case) for idx, bug_case in enumerate(cases)]

        return self._summarize_replay_results(target.repo, results)

    def _summarize_replay_results(
        self,
        repo: str,
        results: list[_BugReplayResult],
    ) -> tuple[int, int, tuple[str, ...], tuple[dict[str, Any], ...]]:
        run_urls: list[str] = [url for res in results for url in res.run_urls]
        if self._replay_verifier is not None:
            bugs_total, bugs_caught, verified_outcomes = self._verify_replay_results(repo, results)
            return bugs_total, bugs_caught, tuple(run_urls), verified_outcomes

        if self._proof_bridge is not None:
            pairs = [res.pair for res in results if res.pair is not None]
            verification = self._proof_bridge.verify_replay_runs(repo, pairs)
            self._set_verified_replays(
                verification.replays,
                getattr(verification, "preview_files", ()),
                getattr(verification, "video_paths", ()),
            )
            conflict_outcomes = tuple(res.outcome for res in results if res.pair is None)
            return (
                verification.bugs_total,
                verification.bugs_caught,
                tuple(run_urls),
                (*verification.bug_outcomes, *conflict_outcomes),
            )

        self._set_verified_replays(())
        bugs_total = sum(1 for res in results if res.counted)
        bugs_caught = sum(1 for res in results if res.counted and res.caught)
        bug_outcomes = tuple(res.outcome for res in results)
        return bugs_total, bugs_caught, tuple(run_urls), bug_outcomes

    def _verify_replay_results(
        self,
        repo: str,
        results: list[_BugReplayResult],
    ) -> tuple[int, int, tuple[dict[str, Any], ...]]:
        assert self._replay_verifier is not None  # noqa: S101  guarded by caller
        paired: list[tuple[int, ReplayRunPair, _ReplayPairMeta]] = [
            (idx, result.pair, result.pair_meta)
            for idx, result in enumerate(results)
            if result.pair is not None and result.pair_meta is not None
        ]
        outcomes: list[dict[str, Any]] = [result.outcome for result in results]
        if not paired:
            self._set_verified_replays(())
            return 0, 0, tuple(outcomes)

        verification = self._replay_verifier.verify_replay_runs(repo, [pair for _, pair, _ in paired])
        self._set_verified_replays(
            verification.replays,
            getattr(verification, "preview_files", ()),
            getattr(verification, "video_paths", ()),
        )
        for pos, (result_idx, _pair, meta) in enumerate(paired):
            replay = verification.replays[pos]
            verified_row = dict(verification.bug_outcomes[pos])
            verified_row["kind"] = meta.kind
            verified_row["ref"] = meta.ref
            verified_row["caught"] = replay.caught
            verified_row["run_url"] = meta.candidate_run_url or meta.baseline_run_url
            outcomes[result_idx] = verified_row
        return verification.bugs_total, verification.bugs_caught, tuple(outcomes)

    def _instrument_replay_files(
        self,
        target: _ReplayTarget,
        ref_sha: str,
        files: dict[str, str],
        expected_checks: tuple[str, ...],
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        workflow_path = f".github/workflows/{target.workflow}"
        workflow_yaml = (
            files[workflow_path]
            if workflow_path in files
            else self.github.get_file_content(target.repo, workflow_path, ref_sha)
        )
        aliases = resolve_workflow_check_aliases(workflow_yaml, workflow_path, expected_checks)
        if not self._record_replays:
            return files, aliases
        instrumented = build_replay_overlay(
            workflow_path,
            workflow_yaml,
            files,
            target_checks=expected_checks,
        )
        return instrumented, aliases

    def _replay_bug_case(
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
        *,
        sem: threading.BoundedSemaphore | None = None,
    ) -> _BugReplayResult:
        _validate_bug_case(bug_case, idx)
        if bug_case.kind == "commit":
            return self._replay_commit_case(target, bug_case, idx, sem=sem)
        return self._replay_mutation_case(target, bug_case, idx, sem=sem)

    def _run_ephemeral_branch(
        self,
        target: _ReplayTarget,
        spec: _BranchReplaySpec,
        *,
        sem: threading.BoundedSemaphore | None = None,
    ) -> WorkflowRunResult:
        guard = sem if sem is not None else nullcontext()
        with guard:
            self.github.create_branch(target.repo, spec.branch, spec.base_sha)
            try:
                if spec.commit:
                    self.github.commit_files(target.repo, spec.branch, spec.base_sha, spec.files, spec.message)
                return self._dispatch_and_poll(target.repo, target.workflow, spec.branch)
            finally:
                self.github.delete_branch(target.repo, spec.branch)

    def _run_branch_pair(
        self,
        target: _ReplayTarget,
        base_spec: _BranchReplaySpec,
        cand_spec: _BranchReplaySpec,
        *,
        sem: threading.BoundedSemaphore | None = None,
    ) -> tuple[WorkflowRunResult, WorkflowRunResult]:
        if self.config.max_parallel_runs > 1:
            with ThreadPoolExecutor(max_workers=2) as pool:
                base_fut = pool.submit(self._run_ephemeral_branch, target, base_spec, sem=sem)
                cand_fut = pool.submit(self._run_ephemeral_branch, target, cand_spec, sem=sem)
                return base_fut.result(), cand_fut.result()
        base_run = self._run_ephemeral_branch(target, base_spec, sem=sem)
        cand_run = self._run_ephemeral_branch(target, cand_spec, sem=sem)
        return base_run, cand_run

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
        *,
        sem: threading.BoundedSemaphore | None = None,
    ) -> _BugReplayResult:
        commit_sha = str(bug_case.commit_sha)
        overlay_files, conflicts = self._build_commit_replay_files(target, commit_sha)
        if conflicts:
            return _inconclusive_replay_result("commit", commit_sha, conflicts)

        if self._record_replays or self._replay_verifier is not None:
            return self._replay_commit_pair(target, bug_case, commit_sha, overlay_files, idx, sem=sem)

        cand_spec = _BranchReplaySpec(
            branch=f"pc/replay-{target.candidate.id}-{idx}",
            base_sha=commit_sha,
            files=overlay_files,
            message=f"Pit Crew replay: overlay candidate {target.candidate.id} onto {commit_sha}",
        )
        if self._proof_bridge is not None:
            _, base_aliases = self._instrument_replay_files(target, commit_sha, {}, bug_case.expected_checks)
            _, cand_aliases = self._instrument_replay_files(target, commit_sha, overlay_files, bug_case.expected_checks)
            base_spec = _BranchReplaySpec(
                branch=f"pc/replay-{target.candidate.id}-{idx}-base",
                base_sha=commit_sha,
                files={},
                message="",
                commit=False,
            )
            base_run, run = self._run_branch_pair(target, base_spec, cand_spec, sem=sem)
            urls = tuple(u for u in (base_run.html_url, run.html_url) if u)
            pair = ReplayRunPair(
                sha=commit_sha,
                check=_check_label_for_case(bug_case, target.workflow),
                baseline_conclusion=base_run.conclusion or "",
                candidate_conclusion=run.conclusion or "",
                baseline_run_id=str(base_run.run_id),
                candidate_run_id=str(run.run_id),
                baseline_run_url=base_run.html_url,
                candidate_run_url=run.html_url,
                expected_checks=bug_case.expected_checks,
                check_aliases=tuple(dict.fromkeys((*base_aliases, *cand_aliases))),
            )
        else:
            run = self._run_ephemeral_branch(target, cand_spec, sem=sem)
            urls = (run.html_url,) if run.html_url else ()
            pair = None

        caught = run.conclusion == "failure"
        outcome: dict[str, Any] = {
            "kind": "commit",
            "ref": commit_sha,
            "caught": caught,
            "baseline_failed": True,
            "run_url": run.html_url,
        }
        return _BugReplayResult(counted=True, caught=caught, outcome=outcome, run_urls=urls, pair=pair)

    def _replay_commit_pair(  # noqa: PLR0913
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        commit_sha: str,
        overlay_files: dict[str, str],
        idx: int,
        *,
        sem: threading.BoundedSemaphore | None = None,
    ) -> _BugReplayResult:
        check_name = _primary_check_name(bug_case, target.workflow)
        base_files, base_aliases = self._instrument_replay_files(target, commit_sha, {}, bug_case.expected_checks)
        base_spec = _BranchReplaySpec(
            branch=f"pc/replay-{target.candidate.id}-{idx}-base",
            base_sha=commit_sha,
            files=base_files,
            message=f"Pit Crew commit baseline replay {idx} for candidate {target.candidate.id} at {commit_sha}",
        )
        if self.config.max_parallel_runs > 1:
            cand_files, cand_aliases = self._instrument_replay_files(
                target, commit_sha, overlay_files, bug_case.expected_checks
            )
            cand_spec = _BranchReplaySpec(
                branch=f"pc/replay-{target.candidate.id}-{idx}-cand",
                base_sha=commit_sha,
                files=cand_files,
                message=f"Pit Crew commit candidate replay {idx}: overlay {target.candidate.id} onto {commit_sha}",
            )
            baseline_run, candidate_run = self._run_branch_pair(target, base_spec, cand_spec, sem=sem)
            return _finalize_commit_pair(
                commit_sha=commit_sha,
                check_name=check_name,
                baseline_run=baseline_run,
                candidate_run=candidate_run,
                expected_checks=bug_case.expected_checks,
                check_aliases=tuple(dict.fromkeys((*base_aliases, *cand_aliases))),
            )

        baseline_run = self._run_ephemeral_branch(target, base_spec, sem=sem)
        if baseline_run.conclusion != "failure":
            return _finalize_commit_pair(
                commit_sha=commit_sha,
                check_name=check_name,
                baseline_run=baseline_run,
                candidate_run=None,
                expected_checks=bug_case.expected_checks,
                check_aliases=base_aliases,
            )

        cand_files, cand_aliases = self._instrument_replay_files(
            target, commit_sha, overlay_files, bug_case.expected_checks
        )
        candidate_run = self._run_ephemeral_branch(
            target,
            _BranchReplaySpec(
                branch=f"pc/replay-{target.candidate.id}-{idx}-cand",
                base_sha=commit_sha,
                files=cand_files,
                message=f"Pit Crew commit candidate replay {idx}: overlay {target.candidate.id} onto {commit_sha}",
            ),
            sem=sem,
        )
        return _finalize_commit_pair(
            commit_sha=commit_sha,
            check_name=check_name,
            baseline_run=baseline_run,
            candidate_run=candidate_run,
            expected_checks=bug_case.expected_checks,
            check_aliases=tuple(dict.fromkeys((*base_aliases, *cand_aliases))),
        )

    def _replay_mutation_case(
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
        *,
        sem: threading.BoundedSemaphore | None = None,
    ) -> _BugReplayResult:
        ref = bug_case.reason or ",".join(sorted(bug_case.mutated_files))
        check_label = (
            _primary_check_name(bug_case, target.workflow)
            if (self._replay_verifier is not None or self._record_replays)
            else _check_label_for_case(bug_case, target.workflow)
        )
        base_files, base_aliases = self._instrument_replay_files(
            target,
            target.base_sha,
            dict(bug_case.mutated_files),
            bug_case.expected_checks,
        )
        base_spec = _BranchReplaySpec(
            branch=f"pc/replay-{target.candidate.id}-{idx}-base",
            base_sha=target.base_sha,
            files=base_files,
            message=f"Pit Crew mutation baseline replay {idx} for candidate {target.candidate.id}",
        )
        if self.config.max_parallel_runs > 1:
            return self._replay_mutation_parallel(
                target,
                bug_case,
                idx,
                ref=ref,
                check_label=check_label,
                base_spec=base_spec,
                base_aliases=base_aliases,
                sem=sem,
            )
        return self._replay_mutation_sequential(
            target,
            bug_case,
            idx,
            ref=ref,
            check_label=check_label,
            base_spec=base_spec,
            base_aliases=base_aliases,
            sem=sem,
        )

    def _replay_mutation_sequential(  # noqa: PLR0913
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
        *,
        ref: str,
        check_label: str,
        base_spec: _BranchReplaySpec,
        base_aliases: tuple[str, ...] = (),
        sem: threading.BoundedSemaphore | None = None,
    ) -> _BugReplayResult:
        baseline_run = self._run_ephemeral_branch(target, base_spec, sem=sem)
        run_urls: list[str] = [baseline_run.html_url] if baseline_run.html_url else []
        use_pair_meta = self._replay_verifier is not None
        mutation_sha = f"mutation-{idx}" if use_pair_meta else ref
        if baseline_run.conclusion != "failure":
            pair = ReplayRunPair(
                sha=mutation_sha,
                check=check_label,
                baseline_conclusion=baseline_run.conclusion or "",
                candidate_conclusion="skipped" if use_pair_meta else "",
                baseline_run_id=str(baseline_run.run_id),
                candidate_run_id="",
                baseline_run_url=baseline_run.html_url,
                expected_checks=bug_case.expected_checks,
                check_aliases=base_aliases,
            )
            meta = (
                _ReplayPairMeta(
                    kind="mutation",
                    ref=ref,
                    baseline_run_url=baseline_run.html_url,
                    candidate_run_url="",
                )
                if use_pair_meta
                else None
            )
            outcome: dict[str, Any] = {
                "kind": "mutation",
                "ref": ref,
                "caught": False,
                "baseline_failed": False,
                "run_url": baseline_run.html_url,
            }
            return _BugReplayResult(
                counted=False,
                caught=False,
                outcome=outcome,
                run_urls=tuple(run_urls),
                pair=pair,
                pair_meta=meta,
            )

        overlay_files, conflicts = self._build_mutation_replay_files(target, bug_case.mutated_files)
        if conflicts:
            return _inconclusive_replay_result(
                "mutation",
                ref,
                conflicts,
                run_url=baseline_run.html_url,
                run_urls=tuple(run_urls),
            )

        cand_files, cand_aliases = self._instrument_replay_files(
            target, target.base_sha, overlay_files, bug_case.expected_checks
        )
        cand_spec = _BranchReplaySpec(
            branch=f"pc/replay-{target.candidate.id}-{idx}-cand",
            base_sha=target.base_sha,
            files=cand_files,
            message=f"Pit Crew mutation candidate replay {idx} for candidate {target.candidate.id}",
        )
        candidate_run = self._run_ephemeral_branch(target, cand_spec, sem=sem)
        return _finalize_mutation_pair(
            ref,
            check_label,
            baseline_run,
            candidate_run,
            sha=mutation_sha,
            expected_checks=bug_case.expected_checks,
            check_aliases=tuple(dict.fromkeys((*base_aliases, *cand_aliases))),
            include_meta=use_pair_meta,
        )

    def _replay_mutation_parallel(  # noqa: PLR0913
        self,
        target: _ReplayTarget,
        bug_case: BugCase,
        idx: int,
        *,
        ref: str,
        check_label: str,
        base_spec: _BranchReplaySpec,
        base_aliases: tuple[str, ...] = (),
        sem: threading.BoundedSemaphore | None = None,
    ) -> _BugReplayResult:
        use_pair_meta = self._replay_verifier is not None
        mutation_sha = f"mutation-{idx}" if use_pair_meta else ref
        overlay_files, conflicts = self._build_mutation_replay_files(target, bug_case.mutated_files)
        if conflicts:
            baseline_run = self._run_ephemeral_branch(target, base_spec, sem=sem)
            urls = (baseline_run.html_url,) if baseline_run.html_url else ()
            if baseline_run.conclusion != "failure":
                pair = ReplayRunPair(
                    sha=mutation_sha,
                    check=check_label,
                    baseline_conclusion=baseline_run.conclusion or "",
                    candidate_conclusion="skipped" if use_pair_meta else "",
                    baseline_run_id=str(baseline_run.run_id),
                    candidate_run_id="",
                    baseline_run_url=baseline_run.html_url,
                    expected_checks=bug_case.expected_checks,
                    check_aliases=base_aliases,
                )
                meta = (
                    _ReplayPairMeta(
                        kind="mutation",
                        ref=ref,
                        baseline_run_url=baseline_run.html_url,
                        candidate_run_url="",
                    )
                    if use_pair_meta
                    else None
                )
                dud: dict[str, Any] = {
                    "kind": "mutation",
                    "ref": ref,
                    "caught": False,
                    "baseline_failed": False,
                    "run_url": baseline_run.html_url,
                }
                return _BugReplayResult(
                    counted=False, caught=False, outcome=dud, run_urls=urls, pair=pair, pair_meta=meta
                )
            return _inconclusive_replay_result(
                "mutation",
                ref,
                conflicts,
                run_url=baseline_run.html_url,
                run_urls=urls,
            )

        cand_files, cand_aliases = self._instrument_replay_files(
            target, target.base_sha, overlay_files, bug_case.expected_checks
        )
        cand_spec = _BranchReplaySpec(
            branch=f"pc/replay-{target.candidate.id}-{idx}-cand",
            base_sha=target.base_sha,
            files=cand_files,
            message=f"Pit Crew mutation candidate replay {idx} for candidate {target.candidate.id}",
        )
        baseline_run, candidate_run = self._run_branch_pair(target, base_spec, cand_spec, sem=sem)
        return _finalize_mutation_pair(
            ref,
            check_label,
            baseline_run,
            candidate_run,
            sha=mutation_sha,
            expected_checks=bug_case.expected_checks,
            check_aliases=tuple(dict.fromkeys((*base_aliases, *cand_aliases))),
            include_meta=use_pair_meta,
        )

    def run_cycle(
        self,
        repo: str,
        base_sha: str | None = None,
        *,
        on_progress: ProgressReporter | None = None,
    ) -> Verdict | None:
        """Run one optimization cycle for ``repo`` and return the resulting ``Verdict``, if any."""
        prev_progress = getattr(self._cycle_local, "on_progress", None)
        self._cycle_local.on_progress = on_progress
        self._set_verified_replays(())
        try:
            pending = self._select_pending_candidate(repo)
            if pending is not None:
                pending_base = base_sha or pending.base_sha or self.resolve_base_sha(repo)
                return self._evaluate_candidate(
                    repo,
                    pending_base,
                    pending,
                    self._find_hypothesis(pending.hypothesis_id),
                )

            resolved_base_sha = self.resolve_base_sha(repo, base_sha)
            logger.info("Starting cycle for %s at base_sha=%s", repo, resolved_base_sha[:12])
            hypothesis = self.select_next_hypothesis(repo)
            if hypothesis is None and self._has_inflight_optimizer(repo):
                logger.info("Optimizer lanes still running for %s; not re-analyzing", repo)
                return None
            if hypothesis is None or hypothesis.base_sha != resolved_base_sha:
                self._report_progress("analysis", 0, 0)
                self._run_analysis(repo, resolved_base_sha)
                hypothesis = self.select_next_hypothesis(repo)
            else:
                logger.info("Reusing hypotheses analyzed at base_sha=%s; skipping analysis", resolved_base_sha[:12])

            if hypothesis is None:
                logger.info("No unattempted hypotheses remain for %s", repo)
                return None

            self._report_progress("optimizer", 0, 0)
            hypotheses = [hypothesis, *self._next_hypotheses(repo, self.config.optimizer_fanout)[1:]]
            found = self._run_optimizers(repo, resolved_base_sha, hypotheses)
            if found is None:
                return None
            hypothesis, candidate = found
            return self._evaluate_candidate(repo, resolved_base_sha, candidate, hypothesis)
        finally:
            self._cycle_local.on_progress = prev_progress

    def _run_analysis(self, repo: str, resolved_base_sha: str) -> None:
        ctx = self._resolve_target_context(repo)
        analysis_task = _build_analysis_task(
            repo=repo,
            base_sha=resolved_base_sha,
            workflow_file=ctx.workflow_file,
            prior_attempts=self.ledger.prior_attempts(repo),
            history_repo=ctx.history_repo,
        )
        if self._use_host_workspace and host_workspace.configured_workspace() is None:
            with (
                host_workspace.materialize(ctx.work_repo) as checkout,
                host_workspace.use_workspace(checkout),
            ):
                self.lane_runner("analysis", analysis_task, self.ledger, repo=ctx.work_repo)
        else:
            self.lane_runner("analysis", analysis_task, self.ledger, repo=ctx.work_repo)

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

        if problems := self.guard_candidate(repo, resolved_base_sha, candidate):
            verdict = Verdict(
                candidate_id=candidate.id,
                keep=False,
                speedup=0.0,
                p_value=None,
                reason=f"{CHECKS_LESS_REASON}: {problems[0]}",
            )
            self.ledger.add(verdict)
            return verdict

        candidate = self._publish_candidate(repo, resolved_base_sha, candidate)

        replay_plan = self._existing_replay_plan(candidate.id)
        if replay_plan is None:
            self._report_progress("proof", 0, 0)
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

        default_wf = self._workflow_default_for(repo)
        workflow = _workflow_for_hypothesis(hypothesis, default_wf) if hypothesis is not None else default_wf
        evidence = self.collect_evidence(candidate, replay_plan, resolved_base_sha, workflow=workflow)
        self.ledger.add(evidence)
        self._report_progress("rating", 0, 0)
        verdict = rate(evidence)
        self.ledger.add(verdict)
        logger.info(
            "Verdict for %s candidate=%s: keep=%s speedup=%.2fx p=%s reason=%s",
            repo,
            candidate.id,
            verdict.keep,
            verdict.speedup,
            verdict.p_value,
            verdict.reason,
        )
        if verdict.keep and self.config.auto_open_pr:
            self._open_kept_pull_request(repo, hypothesis, candidate, evidence, verdict)
        return verdict

    def _open_kept_pull_request(
        self,
        repo: str,
        hypothesis: Hypothesis | None,
        candidate: Candidate,
        evidence: Evidence,
        verdict: Verdict,
    ) -> None:
        ctx = self._resolve_target_context(repo)
        opened = None
        if self._proof_bridge is not None:
            try:
                opened = self._proof_bridge.open_kept_pr(
                    candidate,
                    evidence,
                    verdict,
                    self._get_verified_replays(),
                    config=ProofPRConfig(
                        auto_open_pr=True,
                        base_branch=ctx.base_branch,
                        repo=ctx.work_repo,
                        media_base_url=str(getattr(self._proof_bridge, "public_base_url", "") or ""),
                    ),
                    preview_files=self._get_preview_files(),
                )
            except ProofBridgeError as exc:
                if str(exc) != _MISSING_PR_CLIENT_MSG:
                    raise
        if opened is not None:
            self._cycle_local.last_pr_url = getattr(opened, "url", None)
        elif hypothesis is not None:
            self._cycle_local.last_pr_url = self.github.open_pull_request(
                ctx.work_repo,
                title=format_pr_title(hypothesis, candidate, verdict),
                body=format_pr_body(hypothesis, candidate, evidence, verdict),
                head=candidate.branch,
                base=ctx.base_branch,
            )

    def _run_optimizers(
        self,
        repo: str,
        base_sha: str,
        hypotheses: list[Hypothesis],
    ) -> tuple[Hypothesis, Candidate] | None:
        """Run the Optimizer on every hypothesis at once and return the first that yields a candidate.

        Lanes still running when a candidate arrives keep going in the background. Their candidates
        land in the ledger, and a later cycle evaluates them as pending candidates.
        """
        self._inflight_hypothesis_ids.update(hyp.id for hyp in hypotheses)
        pool = ThreadPoolExecutor(max_workers=len(hypotheses), thread_name_prefix="optimizer")
        futures = {
            pool.submit(contextvars.copy_context().run, self._optimize_one, repo, base_sha, hyp): hyp
            for hyp in hypotheses
        }
        pool.shutdown(wait=False)
        first_error: Exception | None = None
        for future in as_completed(futures):
            try:
                candidate = future.result()
            except Exception as exc:  # noqa: BLE001  re-raised below unless another lane succeeds
                first_error = first_error or exc
                continue
            if candidate is not None:
                return futures[future], candidate
        if first_error is not None:
            raise first_error
        return None

    def _has_inflight_optimizer(self, repo: str) -> bool:
        return any(hyp.id in self._inflight_hypothesis_ids for hyp in self.ledger.hypotheses_for_repo(repo))

    def _optimize_one(self, repo: str, base_sha: str, hypothesis: Hypothesis) -> Candidate | None:
        logger.info(
            "Running optimizer for %s hypothesis=%s (%s, est=%.1fs)",
            repo,
            hypothesis.id,
            hypothesis.skill,
            hypothesis.expected_savings_seconds,
        )
        try:
            candidate, completed = self._run_optimizer(repo, base_sha, hypothesis)
            if candidate is None and completed:
                logger.info("Optimizer declined hypothesis %s (%s)", hypothesis.id, hypothesis.skill)
                self._declined_hypothesis_ids.add(hypothesis.id)
        except Exception:
            logger.exception("Optimizer failed for hypothesis %s (%s)", hypothesis.id, hypothesis.skill)
            raise
        finally:
            # Discard last, so the hypothesis is never briefly selectable between in-flight and declined.
            self._inflight_hypothesis_ids.discard(hypothesis.id)
        return candidate

    def _run_optimizer(self, repo: str, base_sha: str, hypothesis: Hypothesis) -> tuple[Candidate | None, bool]:
        work_repo = self._work_repo_for(repo)
        existing_ids = {cand.id for cand in self.ledger.all(Candidate)}
        task = _build_optimizer_task(
            repo=repo,
            base_sha=base_sha,
            hypothesis=hypothesis,
            prior_attempts=self.ledger.prior_attempts(repo),
        )
        try:
            result = self.lane_runner("optimizer", task, self.ledger, repo=work_repo)
        except RuntimeError as exc:
            if type(exc).__name__ == "MaxTurnsExceededError":
                return None, True
            raise
        new_candidates = [
            cand
            for cand in self.ledger.all(Candidate)
            if cand.id not in existing_ids and cand.hypothesis_id == hypothesis.id
        ]
        return (new_candidates[-1] if new_candidates else None), _is_completed_lane_result(result)

    def _run_proof(self, repo: str, base_sha: str, candidate: Candidate) -> tuple[ReplayPlan | None, bool]:
        ctx = self._resolve_target_context(repo)
        proof_repo = ctx.history_repo or repo
        existing_ids = {plan.id for plan in self.ledger.all(ReplayPlan)}
        if self._proof_bridge is not None:
            try:
                _, result = self._proof_bridge.run(candidate, ledger=self.ledger, repo=proof_repo, base_sha=base_sha)
            except ProofBridgeError as exc:
                if str(exc) != _MISSING_RUNNER_MSG:
                    raise
                ctx_proof = self._proof_bridge.prepare_context(candidate, repo=proof_repo, base_sha=base_sha)
                prompt = self._proof_bridge.build_prompt(ctx_proof)
                result = self.lane_runner("proof", prompt, self.ledger, repo=ctx.work_repo)
        else:
            task = _build_proof_task(repo=proof_repo, base_sha=base_sha, candidate=candidate)
            result = self.lane_runner("proof", task, self.ledger, repo=ctx.work_repo)
        new_plans = [
            plan
            for plan in self.ledger.all(ReplayPlan)
            if plan.id not in existing_ids and plan.candidate_id == candidate.id
        ]
        return (new_plans[-1] if new_plans else None), _is_completed_lane_result(result)

    def run_until_sla(
        self,
        repo: str,
        *,
        on_progress: ProgressReporter | None = None,
    ) -> list[Verdict]:
        """Run compounding optimization cycles for ``repo`` until the CI SLA is met or cycles are exhausted."""
        verdicts: list[Verdict] = []
        for _ in range(self.config.max_compounding_cycles):
            declined_before = len(self._declined_hypothesis_ids)
            verdict = self.run_cycle(repo, on_progress=on_progress)
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


def _finalize_commit_pair(  # noqa: PLR0913
    *,
    commit_sha: str,
    check_name: str,
    baseline_run: WorkflowRunResult,
    candidate_run: WorkflowRunResult | None,
    expected_checks: tuple[str, ...],
    check_aliases: tuple[str, ...],
) -> _BugReplayResult:
    if baseline_run.conclusion != "failure" or candidate_run is None:
        base_urls = (baseline_run.html_url,) if baseline_run.html_url else ()
        pair = ReplayRunPair(
            sha=commit_sha,
            check=check_name,
            baseline_conclusion=baseline_run.conclusion or "",
            candidate_conclusion="skipped" if candidate_run is None else (candidate_run.conclusion or ""),
            baseline_run_id=str(baseline_run.run_id),
            candidate_run_id="" if candidate_run is None else str(candidate_run.run_id),
            baseline_run_url=baseline_run.html_url,
            candidate_run_url="" if candidate_run is None else candidate_run.html_url,
            expected_checks=expected_checks,
            check_aliases=check_aliases,
        )
        meta = _ReplayPairMeta(
            kind="commit",
            ref=commit_sha,
            baseline_run_url=baseline_run.html_url,
            candidate_run_url="",
        )
        dud_outcome: dict[str, Any] = {
            "kind": "commit",
            "ref": commit_sha,
            "caught": False,
            "baseline_failed": False,
            "run_url": baseline_run.html_url,
        }
        return _BugReplayResult(
            counted=False,
            caught=False,
            outcome=dud_outcome,
            run_urls=base_urls,
            pair=pair,
            pair_meta=meta,
        )

    urls = tuple(u for u in (baseline_run.html_url, candidate_run.html_url) if u)
    caught = candidate_run.conclusion == "failure"
    pair = ReplayRunPair(
        sha=commit_sha,
        check=check_name,
        baseline_conclusion=baseline_run.conclusion or "",
        candidate_conclusion=candidate_run.conclusion or "",
        baseline_run_id=str(baseline_run.run_id),
        candidate_run_id=str(candidate_run.run_id),
        baseline_run_url=baseline_run.html_url,
        candidate_run_url=candidate_run.html_url,
        expected_checks=expected_checks,
        check_aliases=check_aliases,
    )
    meta = _ReplayPairMeta(
        kind="commit",
        ref=commit_sha,
        baseline_run_url=baseline_run.html_url,
        candidate_run_url=candidate_run.html_url,
    )
    outcome: dict[str, Any] = {
        "kind": "commit",
        "ref": commit_sha,
        "caught": caught,
        "baseline_failed": True,
        "run_url": candidate_run.html_url,
    }
    return _BugReplayResult(
        counted=True,
        caught=caught,
        outcome=outcome,
        run_urls=urls,
        pair=pair,
        pair_meta=meta,
    )


def _finalize_mutation_pair(  # noqa: PLR0913
    ref: str,
    check_label: str,
    baseline_run: WorkflowRunResult,
    candidate_run: WorkflowRunResult,
    *,
    sha: str | None = None,
    expected_checks: tuple[str, ...] = (),
    check_aliases: tuple[str, ...] = (),
    include_meta: bool = False,
) -> _BugReplayResult:
    urls = tuple(u for u in (baseline_run.html_url, candidate_run.html_url) if u)
    pair = ReplayRunPair(
        sha=sha if sha is not None else ref,
        check=check_label,
        baseline_conclusion=baseline_run.conclusion or "",
        candidate_conclusion=candidate_run.conclusion or "",
        baseline_run_id=str(baseline_run.run_id),
        candidate_run_id=str(candidate_run.run_id),
        baseline_run_url=baseline_run.html_url,
        candidate_run_url=candidate_run.html_url,
        expected_checks=expected_checks,
        check_aliases=check_aliases,
    )
    meta = (
        _ReplayPairMeta(
            kind="mutation",
            ref=ref,
            baseline_run_url=baseline_run.html_url,
            candidate_run_url=candidate_run.html_url if baseline_run.conclusion == "failure" else "",
        )
        if include_meta
        else None
    )
    if baseline_run.conclusion != "failure":
        dud_outcome: dict[str, Any] = {
            "kind": "mutation",
            "ref": ref,
            "caught": False,
            "baseline_failed": False,
            "run_url": baseline_run.html_url,
        }
        base_urls = (baseline_run.html_url,) if baseline_run.html_url else ()
        return _BugReplayResult(
            counted=False, caught=False, outcome=dud_outcome, run_urls=base_urls, pair=pair, pair_meta=meta
        )

    caught = candidate_run.conclusion == "failure"
    outcome: dict[str, Any] = {
        "kind": "mutation",
        "ref": ref,
        "caught": caught,
        "baseline_failed": True,
        "run_url": candidate_run.html_url,
    }
    return _BugReplayResult(counted=True, caught=caught, outcome=outcome, run_urls=urls, pair=pair, pair_meta=meta)
