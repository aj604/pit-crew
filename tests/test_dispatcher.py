import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from pathlib import Path
from typing import Any, override

import pytest

from pit_crew import managed_agent, tools
from pit_crew.contracts import BugCase, Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.dispatcher import (
    DISALLOWED_PATH_REASON,
    NO_REPLAY_PLAN_REASON,
    Dispatcher,
    DispatcherConfig,
    LaneRunner,
    format_pr_body,
    format_pr_title,
    next_lane,
)
from pit_crew.github import GitHubClient, WorkflowRunResult
from pit_crew.lanes.analysis import history as analysis_history
from pit_crew.ledger import InMemoryLedger, Ledger
from pit_crew.rater import rate


class _FakeGitHubClient:
    """Offline fake implementing ``GitHubClient`` for Dispatcher unit tests."""

    def __init__(  # noqa: PLR0913
        self,
        default_shas: dict[str, str],
        run_scripts: dict[int, list[WorkflowRunResult]] | None = None,
        *,
        fail_on: dict[str, Exception] | None = None,
        fail_on_call: dict[tuple[str, int], Exception] | None = None,
        file_contents: dict[tuple[str, str, str], str] | None = None,
        require_known_branch: bool = False,
    ) -> None:
        self._default_shas = dict(default_shas)
        self._run_scripts: dict[int, list[WorkflowRunResult]] = {
            run_id: list(results) for run_id, results in (run_scripts or {}).items()
        }
        self._fail_on: dict[str, Exception] = dict(fail_on or {})
        self._fail_on_call: dict[tuple[str, int], Exception] = dict(fail_on_call or {})
        self._file_contents: dict[tuple[str, str, str], str] = dict(file_contents or {})
        self._require_known_branch = require_known_branch
        self._method_calls: dict[str, int] = {}
        self.default_branch_lookups: list[str] = []
        self.dispatches: list[tuple[str, str, str]] = []
        self.poll_calls: list[tuple[str, int]] = []
        self.branches: dict[tuple[str, str], str] = {}
        self.created_branches: list[tuple[str, str, str]] = []
        self.deleted_branches: list[tuple[str, str]] = []
        self.events: list[tuple[str, ...]] = []
        self.commits: list[dict[str, object]] = []
        self.pull_requests: list[dict[str, str]] = []

    def _maybe_fail(self, method: str) -> None:
        call_num = self._method_calls.get(method, 0) + 1
        self._method_calls[method] = call_num
        if method in self._fail_on:
            raise self._fail_on[method]
        if (method, call_num) in self._fail_on_call:
            raise self._fail_on_call[(method, call_num)]

    def get_default_branch_sha(self, repo: str) -> str:
        self.default_branch_lookups.append(repo)
        return self._default_shas[repo]

    def create_branch(self, repo: str, branch: str, base_sha: str) -> str:
        self.events.append(("create_branch", repo, branch, base_sha))
        self._maybe_fail("create_branch")
        self.created_branches.append((repo, branch, base_sha))
        self.branches[(repo, branch)] = base_sha
        return base_sha

    def delete_branch(self, repo: str, branch: str) -> None:
        self.events.append(("delete_branch", repo, branch))
        self.deleted_branches.append((repo, branch))
        del self.branches[(repo, branch)]

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        self.events.append(("commit_files", repo, branch, base_sha))
        self._maybe_fail("commit_files")
        head_sha = f"commit-{len(self.commits) + 1}"
        self.branches[(repo, branch)] = head_sha
        self.commits.append({"repo": repo, "branch": branch, "base_sha": base_sha, "files": files, "message": message})
        return head_sha

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        self.events.append(("dispatch_workflow", repo, workflow, ref))
        self._maybe_fail("dispatch_workflow")
        if self._require_known_branch and (repo, ref) not in self.branches:
            msg = f"Cannot dispatch workflow on unknown or non-branch ref {ref!r} in {repo!r}"
            raise RuntimeError(msg)
        self.dispatches.append((repo, workflow, ref))
        return len(self.dispatches)

    def get_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
        self.events.append(("get_workflow_run", repo, str(run_id)))
        self._maybe_fail("get_workflow_run")
        self.poll_calls.append((repo, run_id))
        script = self._run_scripts.get(run_id)
        if script:
            if len(script) > 1:
                return script.pop(0)
            return script[0]
        return WorkflowRunResult(
            run_id=run_id,
            status="completed",
            conclusion="success",
            duration_seconds=30.0,
            html_url=f"https://github.com/{repo}/actions/runs/{run_id}",
            head_sha=self._default_shas[repo],
            head_branch="main",
        )

    def get_file_content(self, repo: str, path: str, ref: str) -> str | None:
        return self._file_contents.get((repo, ref, path))

    def open_pull_request(self, repo: str, *, title: str, body: str, head: str, base: str) -> str:
        url = f"https://github.com/{repo}/pull/{len(self.pull_requests) + 1}"
        self.pull_requests.append({"repo": repo, "title": title, "body": body, "head": head, "base": base})
        return url


class _FakeClock:
    """Deterministic monotonic clock and sleep hook for polling/timeout tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_run(
    run_id: int,
    *,
    status: str = "completed",
    conclusion: str | None = "success",
    duration_seconds: float = 30.0,
    html_url: str | None = None,
) -> WorkflowRunResult:
    resolved_url = f"https://github.com/org/repo/actions/runs/{run_id}" if html_url is None else html_url
    return WorkflowRunResult(
        run_id=run_id,
        status=status,
        conclusion=conclusion,
        duration_seconds=duration_seconds,
        html_url=resolved_url,
        head_sha="sha",
        head_branch="main",
    )


@dataclass(frozen=True)
class _LaneCall:
    lane: str
    task: str
    repo: str | None


class _RecordingLaneRunner:
    """Offline fake ``LaneRunner`` that records calls and runs per-lane callbacks."""

    def __init__(self, handlers: dict[str, Callable[[Ledger, str, str | None], None]] | None = None) -> None:
        self._handlers = dict(handlers or {})
        self.calls: list[_LaneCall] = []

    def __call__(
        self,
        lane: str,
        task: str,
        ledger: Ledger | None = None,
        *,
        repo: str | None = None,
    ) -> None:
        self.calls.append(_LaneCall(lane=lane, task=task, repo=repo))
        handler = self._handlers.get(lane)
        if handler is not None and ledger is not None:
            handler(ledger, task, repo)


class _SpyEvidenceDispatcher(Dispatcher):
    """Subclass recording arguments passed to ``collect_evidence``."""

    def __init__(
        self,
        ledger: Ledger,
        github: GitHubClient,
        lane_runner: LaneRunner | None = None,
        config: DispatcherConfig | None = None,
    ) -> None:
        super().__init__(ledger, github, lane_runner=lane_runner, config=config)
        self.evidence_calls: list[tuple[Candidate, ReplayPlan, str, str | None]] = []

    @override
    def collect_evidence(
        self,
        candidate: Candidate,
        replay_plan: ReplayPlan,
        base_sha: str,
        *,
        workflow: str | None = None,
    ) -> Evidence:
        self.evidence_calls.append((candidate, replay_plan, base_sha, workflow))
        return super().collect_evidence(candidate, replay_plan, base_sha, workflow=workflow)


def test_next_lane_preserves_existing_behavior() -> None:
    hyp = Hypothesis(repo="org/repo", description="cold cache", skill="dep-cache", expected_savings_seconds=90.0)
    cand = Candidate(hypothesis_id=hyp.id, branch="pc/cache", summary="add cache")
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("abc123",))
    ev = Evidence(
        candidate_id=cand.id,
        baseline_seconds=(100.0,),
        candidate_seconds=(50.0,),
        bugs_total=1,
        bugs_caught=1,
    )
    verdict = Verdict(candidate_id=cand.id, keep=True, speedup=2.0, p_value=0.01, reason="2.00x faster")

    assert next_lane(hyp) == "optimizer"
    assert next_lane(cand) == "proof"
    assert next_lane(plan) is None
    assert next_lane(ev) is None
    assert next_lane(verdict) is None


def test_dispatcher_config_exposes_spec_defaults_and_is_frozen() -> None:
    cfg = DispatcherConfig()

    assert [f.name for f in fields(DispatcherConfig)] == [
        "timing_pairs",
        "max_compounding_cycles",
        "workflow_file",
        "run_timeout_seconds",
        "poll_interval_seconds",
        "auto_open_pr",
        "base_branch",
    ]
    assert cfg.timing_pairs == 5
    assert cfg.max_compounding_cycles == 5
    assert cfg.workflow_file == "ci.yml"
    assert cfg.run_timeout_seconds == 900.0
    assert cfg.poll_interval_seconds == 5.0
    assert cfg.auto_open_pr is True
    assert cfg.base_branch == "main"

    with pytest.raises(FrozenInstanceError):
        cfg.timing_pairs = 10  # type: ignore[misc]


def test_resolve_base_sha_falls_back_to_default_branch_sha() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "default-sha-000"})
    dispatcher = Dispatcher(ledger, github, lane_runner=_RecordingLaneRunner())

    assert dispatcher.resolve_base_sha("org/repo") == "default-sha-000"
    assert github.default_branch_lookups == ["org/repo"]

    # A rejected candidate must not advance base_sha.
    hyp = Hypothesis(repo="org/repo", description="dedupe", skill="dedupe-jobs", expected_savings_seconds=40.0)
    rejected = Candidate(hypothesis_id=hyp.id, branch="pc/rej", summary="rej", repo="org/repo", head_sha="rej-sha")
    ledger.add(hyp)
    ledger.add(rejected)
    ledger.add(Verdict(candidate_id=rejected.id, keep=False, speedup=1.0, p_value=0.5, reason="noise"))

    assert dispatcher.resolve_base_sha("org/repo") == "default-sha-000"

    # A kept candidate with an empty head_sha also falls back to default branch SHA.
    kept_without_sha = Candidate(hypothesis_id=hyp.id, branch="pc/no-sha", summary="kept", repo="org/repo", head_sha="")
    ledger.add(kept_without_sha)
    ledger.add(Verdict(candidate_id=kept_without_sha.id, keep=True, speedup=1.5, p_value=0.01, reason="faster"))

    assert dispatcher.resolve_base_sha("org/repo") == "default-sha-000"


def test_resolve_base_sha_uses_latest_kept_candidate_and_respects_explicit_override() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "default-sha-000"})
    dispatcher = Dispatcher(ledger, github, lane_runner=_RecordingLaneRunner())

    h1 = Hypothesis(repo="org/repo", description="h1", skill="dep-cache", expected_savings_seconds=50.0)
    h2 = Hypothesis(repo="org/repo", description="h2", skill="dedupe-jobs", expected_savings_seconds=60.0)
    h3 = Hypothesis(repo="org/repo", description="h3", skill="shard-by-timing", expected_savings_seconds=70.0)
    other_h = Hypothesis(repo="other/repo", description="other", skill="dep-cache", expected_savings_seconds=200.0)
    for hyp in (h1, h2, h3, other_h):
        ledger.add(hyp)

    c1 = Candidate(hypothesis_id=h1.id, branch="pc/1", summary="c1", repo="org/repo", head_sha="kept-sha-1")
    c2 = Candidate(hypothesis_id=h2.id, branch="pc/2", summary="c2", repo="org/repo", head_sha="kept-sha-2")
    c3 = Candidate(hypothesis_id=h3.id, branch="pc/3", summary="c3", repo="org/repo", head_sha="rejected-sha-3")
    other_c = Candidate(hypothesis_id=other_h.id, branch="pc/o", summary="o", repo="other/repo", head_sha="other-sha")
    for cand in (c1, c2, c3, other_c):
        ledger.add(cand)

    ledger.add(Verdict(candidate_id=c1.id, keep=True, speedup=1.4, p_value=0.01, reason="ok"))
    ledger.add(Verdict(candidate_id=c2.id, keep=True, speedup=1.3, p_value=0.01, reason="ok"))
    ledger.add(Verdict(candidate_id=c3.id, keep=False, speedup=1.01, p_value=0.4, reason="noise"))
    ledger.add(Verdict(candidate_id=other_c.id, keep=True, speedup=2.0, p_value=0.01, reason="ok"))

    assert dispatcher.resolve_base_sha("org/repo") == "kept-sha-2"
    assert dispatcher.resolve_base_sha("org/repo", base_sha="explicit-sha") == "explicit-sha"
    assert github.default_branch_lookups == []


def test_run_cycle_returns_none_when_no_unattempted_hypotheses_exist() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha-1"})

    attempted_h = Hypothesis(
        repo="org/repo", description="already tried", skill="dep-cache", expected_savings_seconds=80.0
    )
    attempted_c = Candidate(hypothesis_id=attempted_h.id, branch="pc/tried", summary="tried", repo="org/repo")
    other_repo_h = Hypothesis(repo="other/repo", description="other", skill="dep-cache", expected_savings_seconds=999.0)
    ledger.add(attempted_h)
    ledger.add(attempted_c)
    ledger.add(Verdict(candidate_id=attempted_c.id, keep=False, speedup=1.0, p_value=0.5, reason="noise"))
    ledger.add(other_repo_h)

    runner = _RecordingLaneRunner()
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    result = dispatcher.run_cycle("org/repo")

    assert result is None
    assert [call.lane for call in runner.calls] == ["analysis"]
    assert runner.calls[0].repo == "org/repo"
    assert "base-sha-1" in runner.calls[0].task
    assert github.dispatches == []


def _parse_task_context(task: str) -> dict[str, Any]:
    _, raw_json = task.split("Context:\n", maxsplit=1)
    parsed = json.loads(raw_json)
    assert isinstance(parsed, dict)
    return parsed


def _seed_kept_candidate(ledger: Ledger, repo: str, head_sha: str) -> Candidate:
    hyp = Hypothesis(repo=repo, description="prior win", skill="dep-cache", expected_savings_seconds=500.0)
    cand = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/kept",
        summary="added uv cache",
        repo=repo,
        base_sha="default-sha",
        head_sha=head_sha,
        changed_files=(".github/workflows/ci.yml",),
    )
    ledger.add(hyp)
    ledger.add(cand)
    ledger.add(Verdict(candidate_id=cand.id, keep=True, speedup=1.6, p_value=0.01, reason="faster"))
    return cand


def test_run_cycle_selects_highest_impact_unattempted_hypothesis_and_runs_lane_handoff() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "default-sha"})
    kept_c = _seed_kept_candidate(ledger, "org/repo", "compounded-sha-1")

    low_h = Hypothesis(
        repo="org/repo",
        description="minor dedupe",
        skill="dedupe-jobs",
        expected_savings_seconds=25.0,
        workflow_path=".github/workflows/lint.yml",
    )
    high_h = Hypothesis(
        repo="org/repo",
        description="shard slow pytest suite",
        skill="shard-by-timing",
        expected_savings_seconds=140.0,
        workflow_path=".github/workflows/tests.yml",
    )
    other_repo_h = Hypothesis(
        repo="other/repo",
        description="huge other repo win",
        skill="dep-cache",
        expected_savings_seconds=999.0,
    )

    created: dict[str, object] = {}

    def on_analysis(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        ctx = _parse_task_context(task)
        assert ctx["repo"] == "org/repo"
        assert ctx["base_sha"] == "compounded-sha-1"
        active_ledger.add(low_h)
        active_ledger.add(high_h)
        active_ledger.add(other_repo_h)

    def on_optimizer(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        ctx = _parse_task_context(task)
        assert ctx["hypothesis"]["id"] == high_h.id
        assert ctx["hypothesis"]["skill"] == "shard-by-timing"
        assert ctx["base_sha"] == "compounded-sha-1"
        attempt_candidate_ids = {attempt["candidate_id"] for attempt in ctx["prior_attempts"]}
        assert kept_c.id in attempt_candidate_ids
        cand = Candidate(
            hypothesis_id=high_h.id,
            branch="pc/shard-tests",
            summary="split pytest across 4 shards",
            repo="org/repo",
            base_sha="compounded-sha-1",
            head_sha="cand-head-2",
            files={".github/workflows/tests.yml": "name: Tests\n", "pyproject.toml": "[tool.pytest]\n"},
        )
        created["candidate"] = cand
        active_ledger.add(cand)

    def on_proof(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        cand = created["candidate"]
        assert isinstance(cand, Candidate)
        ctx = _parse_task_context(task)
        assert ctx["candidate"]["id"] == cand.id
        assert ctx["base_sha"] == "compounded-sha-1"
        plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-commit-1", "red-commit-2"))
        created["plan"] = plan
        active_ledger.add(plan)

    runner = _RecordingLaneRunner({"analysis": on_analysis, "optimizer": on_optimizer, "proof": on_proof})
    dispatcher = _SpyEvidenceDispatcher(ledger, github, lane_runner=runner)

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert [call.lane for call in runner.calls] == ["analysis", "optimizer", "proof"]
    assert [call.repo for call in runner.calls] == ["org/repo", "org/repo", "org/repo"]

    cand = created["candidate"]
    plan = created["plan"]
    assert isinstance(cand, Candidate)
    assert isinstance(plan, ReplayPlan)
    assert dispatcher.evidence_calls == [(cand, plan, "compounded-sha-1", "tests.yml")]
    assert ledger.get(Verdict, verdict.id) == verdict
    assert verdict.candidate_id == cand.id
    assert len(ledger.all(Evidence)) == 1
    assert github.default_branch_lookups == []
    assert github.dispatches == [
        *[
            ("org/repo", "tests.yml", f"pc/baseline-{cand.id}"),
            ("org/repo", "tests.yml", "pc/shard-tests"),
        ]
        * 5,
        ("org/repo", "tests.yml", f"pc/replay-{cand.id}-0"),
        ("org/repo", "tests.yml", f"pc/replay-{cand.id}-1"),
    ]
    assert github.branches == {}


def test_run_cycle_uses_config_workflow_file_when_hypothesis_workflow_path_is_empty() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha-0"})
    hyp = Hypothesis(repo="org/repo", description="cache", skill="dep-cache", expected_savings_seconds=60.0)
    cand = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/cache",
        summary="enable cache",
        repo="org/repo",
        changed_files=(".github/workflows/custom.yml",),
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("bug-1",))

    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    cfg = DispatcherConfig(workflow_file="custom.yml")
    dispatcher = _SpyEvidenceDispatcher(ledger, github, lane_runner=runner, config=cfg)

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert dispatcher.evidence_calls == [(replace(cand, base_sha="base-sha-0"), plan, "base-sha-0", "custom.yml")]
    assert github.dispatches == [
        *[
            ("org/repo", "custom.yml", f"pc/baseline-{cand.id}"),
            ("org/repo", "custom.yml", "pc/cache"),
        ]
        * 5,
        ("org/repo", "custom.yml", f"pc/replay-{cand.id}-0"),
    ]
    assert github.branches == {}


@pytest.mark.parametrize(
    "bad_path",
    [
        "src/app.py",
        "tests/test_app.py",
        "e2e/playwright.config.ts",
        "../.github/workflows/ci.yml",
        "/etc/passwd",
        "README.md",
    ],
)
def test_run_cycle_immediately_rejects_disallowed_candidate_paths_without_proof_or_ci(bad_path: str) -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha-0"})
    hyp = Hypothesis(repo="org/repo", description="speedup", skill="affected-tests", expected_savings_seconds=100.0)
    bad_cand = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/unsafe",
        summary="modified disallowed file",
        repo="org/repo",
        changed_files=(".github/workflows/ci.yml", bad_path),
    )

    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(bad_cand),
            "proof": lambda _a, _t, _r: pytest.fail("proof lane must not run for disallowed candidate paths"),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.candidate_id == bad_cand.id
    assert verdict.keep is False
    assert verdict.speedup == 0.0
    assert verdict.p_value is None
    assert verdict.reason == DISALLOWED_PATH_REASON
    assert ledger.get(Verdict, verdict.id) == verdict
    assert [call.lane for call in runner.calls] == ["analysis", "optimizer"]
    assert ledger.all(ReplayPlan) == []
    assert ledger.all(Evidence) == []
    assert github.dispatches == []


def test_validate_candidate_paths_also_inspects_files_dict_bypass() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha-0"})
    hyp = Hypothesis(repo="org/repo", description="speedup", skill="dep-cache", expected_savings_seconds=80.0)
    sneaky = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/sneaky",
        summary="hides src change in files",
        repo="org/repo",
        changed_files=(".github/workflows/ci.yml",),
        files={".github/workflows/ci.yml": "name: CI\n", "src/pit_crew/rater.py": "def rate(e): ...\n"},
    )
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(sneaky),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason == "disallowed path modified"
    assert [call.lane for call in runner.calls] == ["analysis", "optimizer"]
    assert github.dispatches == []


BASE_CI = "on: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"


def test_candidate_that_checks_less_than_the_real_base_is_rejected_before_publishing() -> None:
    # The agent may claim any `original_files`; the dispatcher re-checks against GitHub's copy at the base.
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha-0"}, file_contents={("org/repo", "base-sha-0", ".github/workflows/ci.yml"): BASE_CI}
    )
    hyp = Hypothesis(repo="org/repo", description="speedup", skill="dep-cache", expected_savings_seconds=80.0)
    weaker = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/weaker",
        summary="faster, by ignoring test failures",
        repo="org/repo",
        files={".github/workflows/ci.yml": BASE_CI.replace("run: pytest", "run: pytest || true")},
    )
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(weaker),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason.startswith("candidate checks less than its base: ")
    assert "ignores the exit code" in verdict.reason
    assert [call.lane for call in runner.calls] == ["analysis", "optimizer"]
    assert github.dispatches == []


def test_guard_candidate_passes_safe_changes_and_new_files() -> None:
    github = _FakeGitHubClient(
        {"org/repo": "base-sha-0"}, file_contents={("org/repo", "base-sha-0", ".github/workflows/ci.yml"): BASE_CI}
    )
    dispatcher = Dispatcher(InMemoryLedger(), github, lane_runner=_RecordingLaneRunner())
    safe = Candidate(
        hypothesis_id="h",
        branch="pc/safe",
        summary="shard",
        repo="org/repo",
        files={
            ".github/workflows/ci.yml": BASE_CI.replace("run: pytest", "run: pytest -n auto"),
            ".github/workflows/new.yml": "name: brand new\n",  # not at the base: nothing to compare
        },
    )
    assert dispatcher.guard_candidate("org/repo", "base-sha-0", safe) == []


def test_run_cycle_returns_none_when_optimizer_or_proof_produces_no_record() -> None:
    # Optimizer bails out without writing a Candidate.
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha-0"})
    hyp = Hypothesis(repo="org/repo", description="hard change", skill="parallelize-dag", expected_savings_seconds=75.0)
    runner = _RecordingLaneRunner({"analysis": lambda active, _t, _r: active.add(hyp)})
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    assert dispatcher.run_cycle("org/repo") is None
    assert [call.lane for call in runner.calls] == ["analysis", "optimizer"]
    assert ledger.all(Verdict) == []

    # Proof bails out without writing a ReplayPlan.
    cand = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/ok",
        summary="safe workflow change",
        repo="org/repo",
        changed_files=(".github/workflows/ci.yml",),
    )
    runner_no_proof = _RecordingLaneRunner({"optimizer": lambda active, _t, _r: active.add(cand)})
    dispatcher_no_proof = Dispatcher(ledger, github, lane_runner=runner_no_proof)

    assert dispatcher_no_proof.run_cycle("org/repo") is None
    assert [call.lane for call in runner_no_proof.calls] == ["analysis", "optimizer", "proof"]
    assert ledger.all(Evidence) == []
    assert ledger.all(Verdict) == []


def test_default_lane_runner_delegates_to_managed_agent_run_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(managed_agent.run_lane, LaneRunner)
    recorded: list[tuple[str, str, Ledger | None, str | None]] = []

    def fake_run_lane(
        lane: str,
        task: str,
        ledger: Ledger | None = None,
        *,
        repo: str | None = None,
    ) -> None:
        recorded.append((lane, task, ledger, repo))

    monkeypatch.setattr(managed_agent, "run_lane", fake_run_lane)

    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha-0"})
    dispatcher = Dispatcher(ledger, github)

    assert dispatcher.run_cycle("org/repo") is None
    assert len(recorded) == 1
    assert recorded[0][0] == "analysis"
    assert recorded[0][2] is ledger
    assert recorded[0][3] == "org/repo"


def test_collect_evidence_interleaves_baseline_and_candidate_pairs_including_cold_run() -> None:
    ledger = InMemoryLedger()
    # Run IDs 1..10 correspond to B1, C1, B2, C2, ..., B5, C5 (including cold Run #1).
    baseline_durations = (180.0, 100.0, 102.0, 98.0, 101.0)
    candidate_durations = (95.0, 50.0, 52.0, 48.0, 51.0)
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        base_run_id = pair_idx * 2 + 1
        cand_run_id = pair_idx * 2 + 2
        run_scripts[base_run_id] = [_make_run(base_run_id, duration_seconds=baseline_durations[pair_idx])]
        run_scripts[cand_run_id] = [_make_run(cand_run_id, duration_seconds=candidate_durations[pair_idx])]

    github = _FakeGitHubClient({"org/repo": "base-sha-1"}, run_scripts=run_scripts)
    clock = _FakeClock()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    cand = Candidate(
        hypothesis_id="hyp-1",
        branch="pc/cache",
        summary="add cache",
        repo="org/repo",
        head_sha="cand-head-1",
    )
    plan = ReplayPlan(candidate_id=cand.id)

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha-1", workflow="ci.yml")

    assert (
        github.dispatches
        == [
            ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
            ("org/repo", "ci.yml", "pc/cache"),
        ]
        * 5
    )
    assert github.branches == {}
    assert evidence.candidate_id == cand.id
    assert evidence.baseline_seconds == baseline_durations
    assert evidence.candidate_seconds == candidate_durations
    assert evidence.run_urls == tuple(f"https://github.com/org/repo/actions/runs/{i}" for i in range(1, 11))
    assert evidence.bugs_total == 0
    assert evidence.bugs_caught == 0
    assert clock.sleeps == []


def test_collect_evidence_resolves_workflow_repo_and_head_sha_fallbacks() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(
        repo="org/repo",
        description="cache",
        skill="dep-cache",
        expected_savings_seconds=80.0,
        workflow_path=".github/workflows/bench.yml",
    )
    ledger.add(hyp)

    github = _FakeGitHubClient({"org/repo": "base-sha"})
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=2, workflow_file="fallback.yml"),
    )

    # Candidate omits repo and branch -> resolves repo & workflow from hypothesis, ref from head_sha.
    cand_from_hyp = Candidate(hypothesis_id=hyp.id, branch="", summary="cache", head_sha="cand-sha-99")
    plan = ReplayPlan(candidate_id=cand_from_hyp.id, bug_shas=())

    dispatcher.collect_evidence(cand_from_hyp, plan, "base-sha")

    assert (
        github.dispatches
        == [
            ("org/repo", "bench.yml", f"pc/baseline-{cand_from_hyp.id}"),
            ("org/repo", "bench.yml", f"pc/candidate-{cand_from_hyp.id}"),
        ]
        * 2
    )
    assert github.branches == {}

    # Candidate without a ledger hypothesis falls back to config.workflow_file; explicit path normalizes to basename.
    github.dispatches.clear()
    orphan = Candidate(hypothesis_id="missing-hyp", branch="pc/orphan", summary="orphan", repo="org/repo")
    dispatcher.collect_evidence(orphan, ReplayPlan(candidate_id=orphan.id), "base-sha")
    assert (
        github.dispatches
        == [
            ("org/repo", "fallback.yml", f"pc/baseline-{orphan.id}"),
            ("org/repo", "fallback.yml", "pc/orphan"),
        ]
        * 2
    )

    github.dispatches.clear()
    dispatcher.collect_evidence(
        orphan, ReplayPlan(candidate_id=orphan.id), "base-sha", workflow=".github/workflows/custom.yml"
    )
    assert (
        github.dispatches
        == [
            ("org/repo", "custom.yml", f"pc/baseline-{orphan.id}"),
            ("org/repo", "custom.yml", "pc/orphan"),
        ]
        * 2
    )

    # Missing repo or missing ref raises ValueError.
    no_repo = Candidate(hypothesis_id="missing-hyp", branch="pc/x", summary="x")
    with pytest.raises(ValueError, match="Cannot resolve repository"):
        dispatcher.collect_evidence(no_repo, ReplayPlan(candidate_id=no_repo.id), "base-sha")

    no_ref = Candidate(hypothesis_id=hyp.id, branch="", summary="x", repo="org/repo", head_sha="")
    with pytest.raises(ValueError, match="neither branch nor head_sha"):
        dispatcher.collect_evidence(no_ref, ReplayPlan(candidate_id=no_ref.id), "base-sha")


def test_poll_workflow_run_sleeps_across_statuses_until_completed() -> None:
    ledger = InMemoryLedger()
    final_run = _make_run(1, status="completed", conclusion="success", duration_seconds=42.5)
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={
            1: [
                _make_run(1, status="queued", conclusion=None, duration_seconds=0.0),
                _make_run(1, status="in_progress", conclusion=None, duration_seconds=10.0),
                _make_run(1, status="in_progress", conclusion=None, duration_seconds=25.0),
                final_run,
            ]
        },
    )
    clock = _FakeClock(start=100.0)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(poll_interval_seconds=7.5, run_timeout_seconds=60.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    result = dispatcher.poll_workflow_run("org/repo", 1)

    assert result == final_run
    assert github.poll_calls == [("org/repo", 1)] * 4
    assert clock.sleeps == [7.5, 7.5, 7.5]


def test_poll_workflow_run_raises_timeout_error_when_run_timeout_exceeded() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, status="in_progress", conclusion=None, duration_seconds=0.0)]},
    )
    clock = _FakeClock(start=10.0)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(poll_interval_seconds=5.0, run_timeout_seconds=15.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    with pytest.raises(TimeoutError, match=r"Workflow run 1 in org/repo did not complete within 15\.0s"):
        dispatcher.poll_workflow_run("org/repo", 1)

    assert github.poll_calls == [("org/repo", 1)] * 4
    assert clock.sleeps == [5.0, 5.0, 5.0]


@pytest.mark.parametrize("bad_conclusion", ["failure", "cancelled", None])
def test_collect_evidence_aborts_candidate_on_non_success_and_rater_rejects(bad_conclusion: str | None) -> None:
    ledger = InMemoryLedger()
    # Pair 1: B1(100s), C1(40s); Pair 2: B2(100s), C2(non-success); Pairs 3-5: B3, B4, B5 only.
    run_scripts = {
        1: [_make_run(1, duration_seconds=100.0)],
        2: [_make_run(2, duration_seconds=40.0)],
        3: [_make_run(3, duration_seconds=101.0)],
        4: [_make_run(4, conclusion=bad_conclusion, duration_seconds=12.0)],
        5: [_make_run(5, duration_seconds=99.0)],
        6: [_make_run(6, duration_seconds=102.0)],
        7: [_make_run(7, duration_seconds=98.0)],
    }
    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )
    cand = Candidate(hypothesis_id="h1", branch="pc/broken", summary="breaks CI", repo="org/repo")
    plan = ReplayPlan(candidate_id=cand.id)

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.dispatches == [
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", "pc/broken"),
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", "pc/broken"),
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
    ]
    assert evidence.baseline_seconds == (100.0, 101.0, 99.0, 102.0, 98.0)
    assert evidence.candidate_seconds == (40.0,)
    assert evidence.run_urls == tuple(f"https://github.com/org/repo/actions/runs/{i}" for i in range(1, 8))

    # Even if bug replays pass, insufficient candidate timings force Rater to reject.
    verdict = rate(replace(evidence, bugs_total=1, bugs_caught=1))
    assert verdict.keep is False
    assert verdict.reason == "needs 5 runs per side"


def test_collect_evidence_aborts_baseline_on_non_success_and_stops_early_when_both_abort() -> None:
    ledger = InMemoryLedger()
    # Baseline fails on pair 2, candidate completes all 5 pairs.
    run_scripts = {
        1: [_make_run(1, duration_seconds=100.0)],
        2: [_make_run(2, duration_seconds=50.0)],
        3: [_make_run(3, conclusion="failure", duration_seconds=15.0)],
        4: [_make_run(4, duration_seconds=49.0)],
        5: [_make_run(5, duration_seconds=51.0)],
        6: [_make_run(6, duration_seconds=48.0)],
        7: [_make_run(7, duration_seconds=52.0)],
    }
    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )
    cand = Candidate(hypothesis_id="h1", branch="pc/fast", summary="faster", repo="org/repo")

    evidence = dispatcher.collect_evidence(cand, ReplayPlan(candidate_id=cand.id), "base-sha", workflow="ci.yml")

    assert github.dispatches == [
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", "pc/fast"),
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", "pc/fast"),
        ("org/repo", "ci.yml", "pc/fast"),
        ("org/repo", "ci.yml", "pc/fast"),
        ("org/repo", "ci.yml", "pc/fast"),
    ]
    assert evidence.baseline_seconds == (100.0,)
    assert evidence.candidate_seconds == (50.0, 49.0, 51.0, 48.0, 52.0)
    assert len(evidence.run_urls) == 7
    verdict = rate(replace(evidence, bugs_total=1, bugs_caught=1))
    assert verdict.keep is False
    assert verdict.reason == "needs 5 runs per side"

    # When both sides abort, remaining pairs are not dispatched; empty html_url is omitted.
    both_abort_scripts = {
        1: [_make_run(1, conclusion="cancelled", duration_seconds=5.0, html_url="")],
        2: [_make_run(2, duration_seconds=40.0)],
        3: [_make_run(3, conclusion="failure", duration_seconds=8.0)],
    }
    github_both = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=both_abort_scripts)
    dispatcher_both = Dispatcher(
        ledger,
        github_both,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )

    both_ev = dispatcher_both.collect_evidence(cand, ReplayPlan(candidate_id=cand.id), "base-sha", workflow="ci.yml")

    assert github_both.dispatches == [
        ("org/repo", "ci.yml", f"pc/baseline-{cand.id}"),
        ("org/repo", "ci.yml", "pc/fast"),
        ("org/repo", "ci.yml", "pc/fast"),
    ]
    assert both_ev.baseline_seconds == ()
    assert both_ev.candidate_seconds == (40.0,)
    assert both_ev.run_urls == (
        "https://github.com/org/repo/actions/runs/2",
        "https://github.com/org/repo/actions/runs/3",
    )


def test_run_cycle_persists_interleaved_evidence_and_verdict_to_ledger() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(
        repo="org/repo",
        description="cache uv",
        skill="dep-cache",
        expected_savings_seconds=50.0,
        workflow_path=".github/workflows/ci-fast.yml",
    )
    cand = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/uv-cache",
        summary="cache uv venv",
        repo="org/repo",
        base_sha="base-sha-0",
        head_sha="cand-sha-0",
        changed_files=(".github/workflows/ci-fast.yml",),
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-1",))

    baseline_durations = (100.0, 98.0, 102.0, 99.0, 101.0)
    candidate_durations = (50.0, 49.0, 51.0, 48.0, 52.0)
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        base_id = pair_idx * 2 + 1
        cand_id = pair_idx * 2 + 2
        if base_id == 1:
            run_scripts[base_id] = [
                _make_run(base_id, status="queued", conclusion=None, duration_seconds=0.0),
                _make_run(base_id, status="in_progress", conclusion=None, duration_seconds=10.0),
                _make_run(base_id, duration_seconds=baseline_durations[pair_idx]),
            ]
        else:
            run_scripts[base_id] = [_make_run(base_id, duration_seconds=baseline_durations[pair_idx])]
        run_scripts[cand_id] = [_make_run(cand_id, duration_seconds=candidate_durations[pair_idx])]
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=20.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    clock = _FakeClock()
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=runner,
        config=DispatcherConfig(timing_pairs=5, poll_interval_seconds=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert ledger.get(Verdict, verdict.id) == verdict
    assert ledger.all(Verdict) == [verdict]
    assert verdict.candidate_id == cand.id
    assert verdict.keep is True
    assert verdict.median_seconds == 50.0
    assert verdict.p95_seconds == pytest.approx(51.8)
    assert verdict.sla_met is True
    assert verdict.reason == "2.00x faster, caught 1/1 bugs (SLA met)"

    stored_evidence = ledger.all(Evidence)
    assert len(stored_evidence) == 1
    ev = stored_evidence[0]
    assert ledger.get(Evidence, ev.id) == ev
    assert ev.candidate_id == cand.id
    assert ev.baseline_seconds == baseline_durations
    assert ev.candidate_seconds == candidate_durations
    assert ev.bugs_total == 1
    assert ev.bugs_caught == 1
    assert ev.bug_outcomes == (
        {
            "kind": "commit",
            "ref": "red-1",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/11",
        },
    )
    assert ev.run_urls == tuple(f"https://github.com/org/repo/actions/runs/{i}" for i in range(1, 12))
    assert clock.sleeps == [2.0, 2.0]
    assert github.branches == {}


def test_run_cycle_rejects_and_persists_evidence_when_candidate_breaks_ci() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(repo="org/repo", description="shard", skill="shard-by-timing", expected_savings_seconds=60.0)
    cand = Candidate(
        hypothesis_id=hyp.id,
        branch="pc/bad-ci",
        summary="broken yaml",
        repo="org/repo",
        changed_files=(".github/workflows/ci.yml",),
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-1",))

    run_scripts = {
        1: [_make_run(1, duration_seconds=90.0)],
        2: [_make_run(2, conclusion="failure", duration_seconds=5.0)],
    }
    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner, config=DispatcherConfig(timing_pairs=5))

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is False
    assert ledger.get(Verdict, verdict.id) == verdict
    stored_evidence = ledger.all(Evidence)
    assert len(stored_evidence) == 1
    assert stored_evidence[0].candidate_seconds == ()
    assert len(stored_evidence[0].baseline_seconds) == 5
    assert stored_evidence[0].bugs_total == 1
    assert len(stored_evidence[0].run_urls) == 7
    assert github.branches == {}


def test_collect_evidence_replays_single_commit_case_with_overlay_and_rater_keeps() -> None:
    ledger = InMemoryLedger()
    baseline_durations = (120.0, 100.0, 102.0, 99.0, 101.0)
    candidate_durations = (60.0, 48.0, 50.0, 49.0, 51.0)
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        run_scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=baseline_durations[pair_idx])]
        run_scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=candidate_durations[pair_idx])]
    run_scripts[11] = [
        _make_run(11, status="queued", conclusion=None, duration_seconds=0.0),
        _make_run(11, status="in_progress", conclusion=None, duration_seconds=8.0),
        _make_run(11, status="completed", conclusion="failure", duration_seconds=18.0),
    ]

    github = _FakeGitHubClient({"org/repo": "base-sha-1"}, run_scripts=run_scripts)
    clock = _FakeClock()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5, poll_interval_seconds=3.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    cand_files = {
        ".github/workflows/ci.yml": "name: Fast CI\non: [push, workflow_dispatch]\n",
        "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-n auto'\n",
    }
    cand = Candidate(
        id="cand123",
        hypothesis_id="hyp-1",
        branch="pc/fast-ci",
        summary="enable xdist",
        repo="org/repo",
        head_sha="cand-head-1",
        files=cand_files,
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="commit", commit_sha="deadbeef01", reason="historical red commit"),),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha-1", workflow="ci.yml")

    expected_branch = "pc/replay-cand123-0"
    assert github.created_branches == [
        ("org/repo", "pc/baseline-cand123", "base-sha-1"),
        ("org/repo", expected_branch, "deadbeef01"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/baseline-cand123"),
        ("org/repo", expected_branch),
    ]
    assert github.branches == {}
    assert len(github.commits) == 1
    assert github.commits[0]["repo"] == "org/repo"
    assert github.commits[0]["branch"] == expected_branch
    assert github.commits[0]["base_sha"] == "deadbeef01"
    assert github.commits[0]["files"] == cand_files
    assert github.commits[0]["files"] is not cand.files

    replay_events = github.events[-7:]
    assert replay_events == [
        ("create_branch", "org/repo", expected_branch, "deadbeef01"),
        ("commit_files", "org/repo", expected_branch, "deadbeef01"),
        ("dispatch_workflow", "org/repo", "ci.yml", expected_branch),
        ("get_workflow_run", "org/repo", "11"),
        ("get_workflow_run", "org/repo", "11"),
        ("get_workflow_run", "org/repo", "11"),
        ("delete_branch", "org/repo", expected_branch),
    ]
    assert clock.sleeps == [3.0, 3.0]

    assert evidence.bugs_total == 1
    assert evidence.bugs_caught == 1
    assert evidence.bug_outcomes == (
        {
            "kind": "commit",
            "ref": "deadbeef01",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/11",
        },
    )
    assert evidence.run_urls == tuple(f"https://github.com/org/repo/actions/runs/{i}" for i in range(1, 12))

    verdict = rate(evidence)
    assert verdict.keep is True
    assert verdict.reason == "2.02x faster, caught 1/1 bugs (SLA met)"


def test_collect_evidence_replays_mixed_commit_and_mutation_cases() -> None:
    ledger = InMemoryLedger()
    run_scripts = {
        1: [_make_run(1, conclusion="failure", duration_seconds=15.0)],
        2: [_make_run(2, conclusion="failure", duration_seconds=14.0)],
        3: [_make_run(3, conclusion="failure", duration_seconds=12.0)],
        4: [_make_run(4, conclusion="failure", duration_seconds=17.0, html_url="")],
    }
    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand_files = {".github/workflows/ci.yml": "name: Optimized\n"}
    mutated_files = {"src/app.py": "raise RuntimeError('bug')\n"}
    cand = Candidate(
        id="cand-multi",
        hypothesis_id="h1",
        branch="pc/opt",
        summary="optimize",
        repo="org/repo",
        files=cand_files,
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(kind="commit", commit_sha="red-sha-a", reason="bug A"),
            BugCase(kind="mutation", mutated_files=mutated_files, reason="mutant"),
            BugCase(kind="commit", commit_sha="red-sha-b", reason="bug B"),
        ),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [
        ("org/repo", "pc/replay-cand-multi-0", "red-sha-a"),
        ("org/repo", "pc/replay-cand-multi-1-base", "base-sha"),
        ("org/repo", "pc/replay-cand-multi-1-cand", "base-sha"),
        ("org/repo", "pc/replay-cand-multi-2", "red-sha-b"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/replay-cand-multi-0"),
        ("org/repo", "pc/replay-cand-multi-1-base"),
        ("org/repo", "pc/replay-cand-multi-1-cand"),
        ("org/repo", "pc/replay-cand-multi-2"),
    ]
    assert github.branches == {}
    assert [(c["branch"], c["base_sha"], c["files"]) for c in github.commits] == [
        ("pc/replay-cand-multi-0", "red-sha-a", cand_files),
        ("pc/replay-cand-multi-1-base", "base-sha", mutated_files),
        ("pc/replay-cand-multi-1-cand", "base-sha", mutated_files | cand_files),
        ("pc/replay-cand-multi-2", "red-sha-b", cand_files),
    ]
    assert github.dispatches == [
        ("org/repo", "ci.yml", "pc/replay-cand-multi-0"),
        ("org/repo", "ci.yml", "pc/replay-cand-multi-1-base"),
        ("org/repo", "ci.yml", "pc/replay-cand-multi-1-cand"),
        ("org/repo", "ci.yml", "pc/replay-cand-multi-2"),
    ]
    assert evidence.bugs_total == 3
    assert evidence.bugs_caught == 3
    assert evidence.run_urls == (
        "https://github.com/org/repo/actions/runs/1",
        "https://github.com/org/repo/actions/runs/2",
        "https://github.com/org/repo/actions/runs/3",
    )
    assert evidence.bug_outcomes == (
        {
            "kind": "commit",
            "ref": "red-sha-a",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/1",
        },
        {
            "kind": "mutation",
            "ref": "mutant",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/3",
        },
        {
            "kind": "commit",
            "ref": "red-sha-b",
            "caught": True,
            "baseline_failed": True,
            "run_url": "",
        },
    )


@pytest.mark.parametrize("uncaught_conclusion", ["success", "cancelled", "timed_out", "skipped", None])
def test_collect_evidence_non_failure_replay_is_uncaught_and_rater_rejects(
    uncaught_conclusion: str | None,
) -> None:
    ledger = InMemoryLedger()
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        run_scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=100.0 + pair_idx)]
        run_scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=45.0 + pair_idx)]
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=12.0)]
    run_scripts[12] = [_make_run(12, conclusion=uncaught_conclusion, duration_seconds=14.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )
    cand = Candidate(
        id="cand-miss",
        hypothesis_id="h1",
        branch="pc/miss",
        summary="skips broken test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Shallow CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(kind="commit", commit_sha="caught-sha", reason="caught"),
            BugCase(kind="commit", commit_sha="missed-sha", reason="missed"),
        ),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert evidence.bugs_total == 2
    assert evidence.bugs_caught == 1
    assert evidence.bug_outcomes == (
        {
            "kind": "commit",
            "ref": "caught-sha",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/11",
        },
        {
            "kind": "commit",
            "ref": "missed-sha",
            "caught": False,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/12",
        },
    )
    assert evidence.run_urls[-2:] == (
        "https://github.com/org/repo/actions/runs/11",
        "https://github.com/org/repo/actions/runs/12",
    )
    assert github.branches == {}

    verdict = rate(evidence)
    assert verdict.keep is False
    assert verdict.reason == "caught 1/2 bugs"


@pytest.mark.parametrize("failing_stage", ["commit_files", "dispatch_workflow", "get_workflow_run"])
def test_collect_evidence_deletes_replay_branch_when_replay_stage_raises(failing_stage: str) -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        fail_on={failing_stage: RuntimeError(f"injected {failing_stage} error")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-err",
        hypothesis_id="h1",
        branch="pc/err",
        summary="err test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="commit", commit_sha="bug-err-sha"),),
    )

    with pytest.raises(RuntimeError, match=f"injected {failing_stage} error"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [("org/repo", "pc/replay-cand-err-0", "bug-err-sha")]
    assert github.deleted_branches == [("org/repo", "pc/replay-cand-err-0")]
    assert github.branches == {}


def test_collect_evidence_deletes_replay_branch_on_poll_timeout() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, status="in_progress", conclusion=None, duration_seconds=0.0)]},
    )
    clock = _FakeClock()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0, poll_interval_seconds=5.0, run_timeout_seconds=10.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    cand = Candidate(
        id="cand-timeout",
        hypothesis_id="h1",
        branch="pc/timeout",
        summary="timeout test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="commit", commit_sha="bug-timeout-sha"),),
    )

    with pytest.raises(TimeoutError, match=r"Workflow run 1 in org/repo did not complete within 10\.0s"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [("org/repo", "pc/replay-cand-timeout-0", "bug-timeout-sha")]
    assert github.deleted_branches == [("org/repo", "pc/replay-cand-timeout-0")]
    assert github.branches == {}


def test_collect_evidence_does_not_delete_branch_when_create_branch_fails() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        fail_on={"create_branch": RuntimeError("branch create failed")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-create-err",
        hypothesis_id="h1",
        branch="pc/create-err",
        summary="create err",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="commit", commit_sha="bug-sha"),),
    )

    with pytest.raises(RuntimeError, match="branch create failed"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == []
    assert github.deleted_branches == []
    assert github.branches == {}


def test_collect_evidence_raises_value_error_for_commit_case_missing_sha() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha"})
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(id="cand-nosha", hypothesis_id="h1", branch="pc/nosha", summary="nosha", repo="org/repo")
    malformed_case = BugCase(kind="commit", commit_sha="valid-sha")
    object.__setattr__(malformed_case, "commit_sha", None)
    plan = ReplayPlan(candidate_id=cand.id, cases=(malformed_case,))

    with pytest.raises(ValueError, match="missing commit_sha"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == []
    assert github.deleted_branches == []


def _timing_run_scripts(
    baseline_durations: tuple[float, ...] = (120.0, 100.0, 102.0, 99.0, 101.0),
    candidate_durations: tuple[float, ...] = (60.0, 48.0, 50.0, 49.0, 51.0),
) -> dict[int, list[WorkflowRunResult]]:
    scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx, (base_dur, cand_dur) in enumerate(zip(baseline_durations, candidate_durations, strict=True)):
        scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=base_dur)]
        scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=cand_dur)]
    return scripts


def test_collect_evidence_replays_valid_caught_mutation_and_rater_keeps() -> None:
    ledger = InMemoryLedger()
    run_scripts = _timing_run_scripts()
    run_scripts[11] = [
        _make_run(11, status="queued", conclusion=None, duration_seconds=0.0),
        _make_run(11, status="completed", conclusion="failure", duration_seconds=16.0),
    ]
    run_scripts[12] = [
        _make_run(12, status="in_progress", conclusion=None, duration_seconds=5.0),
        _make_run(12, status="completed", conclusion="failure", duration_seconds=11.0),
    ]

    base_pyproject = "[tool.ruff]\nline-length = 120\n\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    github = _FakeGitHubClient(
        {"org/repo": "base-sha-1"},
        run_scripts=run_scripts,
        file_contents={("org/repo", "base-sha-1", "pyproject.toml"): base_pyproject},
    )
    clock = _FakeClock()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5, poll_interval_seconds=4.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    cand_files = {
        ".github/workflows/ci.yml": "name: Fast CI\n",
        "pyproject.toml": "[tool.ruff]\nline-length = 120\n\n[tool.pytest.ini_options]\naddopts = '-q -n auto'\n",
    }
    mutated_files = {
        "src/app.py": "def add(a, b): return a - b\n",
        "pyproject.toml": "[tool.ruff]\nline-length = 10\n\n[tool.pytest.ini_options]\naddopts = '-q'\n",
    }
    cand = Candidate(
        id="cand-mut",
        hypothesis_id="h1",
        branch="pc/fast-ci",
        summary="parallelize tests",
        repo="org/repo",
        base_sha="base-sha-1",
        head_sha="cand-head-1",
        files=cand_files,
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files=mutated_files, reason="invert add operator"),),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha-1", workflow="ci.yml")

    base_branch = "pc/replay-cand-mut-0-base"
    cand_branch = "pc/replay-cand-mut-0-cand"
    assert github.created_branches == [
        ("org/repo", "pc/baseline-cand-mut", "base-sha-1"),
        ("org/repo", base_branch, "base-sha-1"),
        ("org/repo", cand_branch, "base-sha-1"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/baseline-cand-mut"),
        ("org/repo", base_branch),
        ("org/repo", cand_branch),
    ]
    assert github.branches == {}
    assert len(github.commits) == 2
    assert github.commits[0]["branch"] == base_branch
    assert github.commits[0]["base_sha"] == "base-sha-1"
    assert github.commits[0]["files"] == mutated_files
    assert github.commits[0]["files"] is not mutated_files
    assert github.commits[1]["branch"] == cand_branch
    assert github.commits[1]["base_sha"] == "base-sha-1"
    assert github.commits[1]["files"] == {
        "src/app.py": "def add(a, b): return a - b\n",
        ".github/workflows/ci.yml": "name: Fast CI\n",
        "pyproject.toml": "[tool.ruff]\nline-length = 10\n\n[tool.pytest.ini_options]\naddopts = '-q -n auto'\n",
    }
    assert mutated_files["pyproject.toml"] == (
        "[tool.ruff]\nline-length = 10\n\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    )
    assert ".github/workflows/ci.yml" not in mutated_files

    assert github.events[-12:] == [
        ("create_branch", "org/repo", base_branch, "base-sha-1"),
        ("commit_files", "org/repo", base_branch, "base-sha-1"),
        ("dispatch_workflow", "org/repo", "ci.yml", base_branch),
        ("get_workflow_run", "org/repo", "11"),
        ("get_workflow_run", "org/repo", "11"),
        ("delete_branch", "org/repo", base_branch),
        ("create_branch", "org/repo", cand_branch, "base-sha-1"),
        ("commit_files", "org/repo", cand_branch, "base-sha-1"),
        ("dispatch_workflow", "org/repo", "ci.yml", cand_branch),
        ("get_workflow_run", "org/repo", "12"),
        ("get_workflow_run", "org/repo", "12"),
        ("delete_branch", "org/repo", cand_branch),
    ]
    assert clock.sleeps == [4.0, 4.0]

    assert evidence.bugs_total == 1
    assert evidence.bugs_caught == 1
    assert evidence.bug_outcomes == (
        {
            "kind": "mutation",
            "ref": "invert add operator",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/12",
        },
    )
    assert evidence.run_urls == tuple(f"https://github.com/org/repo/actions/runs/{i}" for i in range(1, 13))

    verdict = rate(evidence)
    assert verdict.keep is True
    assert verdict.reason == "2.02x faster, caught 1/1 bugs (SLA met)"


def test_collect_evidence_mutation_ref_falls_back_to_sorted_files_and_omits_empty_urls() -> None:
    ledger = InMemoryLedger()
    run_scripts = {
        1: [_make_run(1, conclusion="failure", duration_seconds=10.0, html_url="")],
        2: [_make_run(2, conclusion="failure", duration_seconds=9.0, html_url="")],
        3: [_make_run(3, conclusion="success", duration_seconds=10.0, html_url="")],
    }
    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-ref",
        hypothesis_id="h1",
        branch="pc/ref",
        summary="ref fallback",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(
                kind="mutation",
                mutated_files={"src/z.py": "x = 1\n", "src/a.py": "y = 2\n"},
            ),
            BugCase(
                kind="mutation",
                mutated_files={"src/dud.py": "# noop\n"},
            ),
        ),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert evidence.bugs_total == 1
    assert evidence.bugs_caught == 1
    assert evidence.run_urls == ()
    assert evidence.bug_outcomes == (
        {
            "kind": "mutation",
            "ref": "src/a.py,src/z.py",
            "caught": True,
            "baseline_failed": True,
            "run_url": "",
        },
        {
            "kind": "mutation",
            "ref": "src/dud.py",
            "caught": False,
            "baseline_failed": False,
            "run_url": "",
        },
    )
    assert github.branches == {}


@pytest.mark.parametrize("uncaught_conclusion", ["success", "cancelled", "timed_out", "skipped", None])
def test_collect_evidence_valid_uncaught_mutation_rejected_by_rater(uncaught_conclusion: str | None) -> None:
    ledger = InMemoryLedger()
    run_scripts = _timing_run_scripts()
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=15.0)]
    run_scripts[12] = [_make_run(12, conclusion=uncaught_conclusion, duration_seconds=10.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )
    cand = Candidate(
        id="cand-miss-mut",
        hypothesis_id="h1",
        branch="pc/miss-mut",
        summary="skips unit tests",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Shallow CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/auth.py": "return True\n"}, reason="auth bypass"),),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert evidence.bugs_total == 1
    assert evidence.bugs_caught == 0
    assert evidence.bug_outcomes == (
        {
            "kind": "mutation",
            "ref": "auth bypass",
            "caught": False,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/12",
        },
    )
    assert evidence.run_urls[-2:] == (
        "https://github.com/org/repo/actions/runs/11",
        "https://github.com/org/repo/actions/runs/12",
    )
    assert github.deleted_branches == [
        ("org/repo", "pc/baseline-cand-miss-mut"),
        ("org/repo", "pc/replay-cand-miss-mut-0-base"),
        ("org/repo", "pc/replay-cand-miss-mut-0-cand"),
    ]
    assert github.branches == {}

    verdict = rate(evidence)
    assert verdict.keep is False
    assert verdict.reason == "caught 0/1 bugs"


@pytest.mark.parametrize("dud_conclusion", ["success", "cancelled", "timed_out", "skipped", None])
def test_collect_evidence_excludes_dud_mutation_without_running_candidate(dud_conclusion: str | None) -> None:
    ledger = InMemoryLedger()
    run_scripts = _timing_run_scripts()
    # Run 11: dud mutation baseline does not fail -> candidate must NOT be created/dispatched.
    # Run 12 & 13: valid mutation baseline fails, candidate fails -> counted and caught.
    run_scripts[11] = [_make_run(11, conclusion=dud_conclusion, duration_seconds=12.0)]
    run_scripts[12] = [_make_run(12, conclusion="failure", duration_seconds=14.0)]
    run_scripts[13] = [_make_run(13, conclusion="failure", duration_seconds=11.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )
    cand = Candidate(
        id="cand-dud-mix",
        hypothesis_id="h1",
        branch="pc/fast",
        summary="cache deps",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Cached CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(kind="mutation", mutated_files={"src/dead_code.py": "x = 1\n"}, reason="dud mutant"),
            BugCase(kind="mutation", mutated_files={"src/core.py": "raise ValueError()\n"}, reason="real mutant"),
        ),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [
        ("org/repo", "pc/baseline-cand-dud-mix", "base-sha"),
        ("org/repo", "pc/replay-cand-dud-mix-0-base", "base-sha"),
        ("org/repo", "pc/replay-cand-dud-mix-1-base", "base-sha"),
        ("org/repo", "pc/replay-cand-dud-mix-1-cand", "base-sha"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/baseline-cand-dud-mix"),
        ("org/repo", "pc/replay-cand-dud-mix-0-base"),
        ("org/repo", "pc/replay-cand-dud-mix-1-base"),
        ("org/repo", "pc/replay-cand-dud-mix-1-cand"),
    ]
    assert github.branches == {}
    assert all(ref != "pc/replay-cand-dud-mix-0-cand" for _, _, ref in github.dispatches)

    assert evidence.bugs_total == 1
    assert evidence.bugs_caught == 1
    assert evidence.bug_outcomes == (
        {
            "kind": "mutation",
            "ref": "dud mutant",
            "caught": False,
            "baseline_failed": False,
            "run_url": "https://github.com/org/repo/actions/runs/11",
        },
        {
            "kind": "mutation",
            "ref": "real mutant",
            "caught": True,
            "baseline_failed": True,
            "run_url": "https://github.com/org/repo/actions/runs/13",
        },
    )
    assert evidence.run_urls[-3:] == (
        "https://github.com/org/repo/actions/runs/11",
        "https://github.com/org/repo/actions/runs/12",
        "https://github.com/org/repo/actions/runs/13",
    )

    verdict = rate(evidence)
    assert verdict.keep is True
    assert verdict.reason == "2.02x faster, caught 1/1 bugs (SLA met)"


def test_collect_evidence_all_dud_mutations_rejected_by_rater() -> None:
    ledger = InMemoryLedger()
    run_scripts = _timing_run_scripts()
    run_scripts[11] = [_make_run(11, conclusion="success", duration_seconds=12.0)]
    run_scripts[12] = [_make_run(12, conclusion="success", duration_seconds=13.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=5),
    )
    cand = Candidate(
        id="cand-all-duds",
        hypothesis_id="h1",
        branch="pc/fast",
        summary="fast ci",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Fast CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(kind="mutation", mutated_files={"src/a.py": "# comment\n"}, reason="dud 1"),
            BugCase(kind="mutation", mutated_files={"src/b.py": "# comment\n"}, reason="dud 2"),
        ),
    )

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [
        ("org/repo", "pc/baseline-cand-all-duds", "base-sha"),
        ("org/repo", "pc/replay-cand-all-duds-0-base", "base-sha"),
        ("org/repo", "pc/replay-cand-all-duds-1-base", "base-sha"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/baseline-cand-all-duds"),
        ("org/repo", "pc/replay-cand-all-duds-0-base"),
        ("org/repo", "pc/replay-cand-all-duds-1-base"),
    ]
    assert github.branches == {}
    assert evidence.bugs_total == 0
    assert evidence.bugs_caught == 0
    assert evidence.bug_outcomes == (
        {
            "kind": "mutation",
            "ref": "dud 1",
            "caught": False,
            "baseline_failed": False,
            "run_url": "https://github.com/org/repo/actions/runs/11",
        },
        {
            "kind": "mutation",
            "ref": "dud 2",
            "caught": False,
            "baseline_failed": False,
            "run_url": "https://github.com/org/repo/actions/runs/12",
        },
    )
    assert evidence.run_urls[-2:] == (
        "https://github.com/org/repo/actions/runs/11",
        "https://github.com/org/repo/actions/runs/12",
    )

    verdict = rate(evidence)
    assert verdict.keep is False
    assert verdict.reason == "no bugs replayed, so safety is unproven"


@pytest.mark.parametrize("failing_stage", ["commit_files", "dispatch_workflow", "get_workflow_run"])
def test_collect_evidence_deletes_mutation_baseline_branch_when_stage_raises(failing_stage: str) -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        fail_on_call={(failing_stage, 1): RuntimeError(f"baseline {failing_stage} error")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-mbase-err",
        hypothesis_id="h1",
        branch="pc/err",
        summary="err test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="div zero"),),
    )

    with pytest.raises(RuntimeError, match=f"baseline {failing_stage} error"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [("org/repo", "pc/replay-cand-mbase-err-0-base", "base-sha")]
    assert github.deleted_branches == [("org/repo", "pc/replay-cand-mbase-err-0-base")]
    assert github.branches == {}


def test_collect_evidence_deletes_mutation_baseline_branch_on_poll_timeout() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, status="in_progress", conclusion=None, duration_seconds=0.0)]},
    )
    clock = _FakeClock()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0, poll_interval_seconds=5.0, run_timeout_seconds=10.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    cand = Candidate(
        id="cand-mbase-timeout",
        hypothesis_id="h1",
        branch="pc/timeout",
        summary="timeout test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="div zero"),),
    )

    with pytest.raises(TimeoutError, match=r"Workflow run 1 in org/repo did not complete within 10\.0s"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [("org/repo", "pc/replay-cand-mbase-timeout-0-base", "base-sha")]
    assert github.deleted_branches == [("org/repo", "pc/replay-cand-mbase-timeout-0-base")]
    assert github.branches == {}


def test_collect_evidence_does_not_delete_mutation_baseline_branch_when_create_branch_fails() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        fail_on_call={("create_branch", 1): RuntimeError("baseline create failed")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-mbase-create",
        hypothesis_id="h1",
        branch="pc/create",
        summary="create err",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="div zero"),),
    )

    with pytest.raises(RuntimeError, match="baseline create failed"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == []
    assert github.deleted_branches == []
    assert github.branches == {}


@pytest.mark.parametrize("failing_stage", ["commit_files", "dispatch_workflow", "get_workflow_run"])
def test_collect_evidence_deletes_mutation_candidate_branch_when_stage_raises(failing_stage: str) -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, conclusion="failure", duration_seconds=10.0)]},
        fail_on_call={(failing_stage, 2): RuntimeError(f"candidate {failing_stage} error")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-mcand-err",
        hypothesis_id="h1",
        branch="pc/err",
        summary="err test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="div zero"),),
    )

    with pytest.raises(RuntimeError, match=f"candidate {failing_stage} error"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [
        ("org/repo", "pc/replay-cand-mcand-err-0-base", "base-sha"),
        ("org/repo", "pc/replay-cand-mcand-err-0-cand", "base-sha"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/replay-cand-mcand-err-0-base"),
        ("org/repo", "pc/replay-cand-mcand-err-0-cand"),
    ]
    assert github.branches == {}


def test_collect_evidence_deletes_mutation_candidate_branch_on_poll_timeout() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={
            1: [_make_run(1, conclusion="failure", duration_seconds=10.0)],
            2: [_make_run(2, status="in_progress", conclusion=None, duration_seconds=0.0)],
        },
    )
    clock = _FakeClock()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0, poll_interval_seconds=5.0, run_timeout_seconds=10.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    cand = Candidate(
        id="cand-mcand-timeout",
        hypothesis_id="h1",
        branch="pc/timeout",
        summary="timeout test",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="div zero"),),
    )

    with pytest.raises(TimeoutError, match=r"Workflow run 2 in org/repo did not complete within 10\.0s"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [
        ("org/repo", "pc/replay-cand-mcand-timeout-0-base", "base-sha"),
        ("org/repo", "pc/replay-cand-mcand-timeout-0-cand", "base-sha"),
    ]
    assert github.deleted_branches == [
        ("org/repo", "pc/replay-cand-mcand-timeout-0-base"),
        ("org/repo", "pc/replay-cand-mcand-timeout-0-cand"),
    ]
    assert github.branches == {}


def test_collect_evidence_does_not_delete_mutation_candidate_branch_when_create_branch_fails() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, conclusion="failure", duration_seconds=10.0)]},
        fail_on_call={("create_branch", 2): RuntimeError("candidate create failed")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-mcand-create",
        hypothesis_id="h1",
        branch="pc/create",
        summary="create err",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"src/app.py": "1/0\n"}, reason="div zero"),),
    )

    with pytest.raises(RuntimeError, match="candidate create failed"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == [("org/repo", "pc/replay-cand-mcand-create-0-base", "base-sha")]
    assert github.deleted_branches == [("org/repo", "pc/replay-cand-mcand-create-0-base")]
    assert github.branches == {}


def test_collect_evidence_raises_value_error_for_malformed_mutation_and_unknown_kind() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/repo": "base-sha"})
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(id="cand-badcase", hypothesis_id="h1", branch="pc/badcase", summary="badcase", repo="org/repo")

    empty_mutation = BugCase(kind="mutation", mutated_files={"src/a.py": "x\n"})
    object.__setattr__(empty_mutation, "mutated_files", {})
    with pytest.raises(ValueError, match="missing mutated_files"):
        dispatcher.collect_evidence(cand, ReplayPlan(candidate_id=cand.id, cases=(empty_mutation,)), "base-sha")

    unknown_kind = BugCase(kind="commit", commit_sha="valid-sha")
    object.__setattr__(unknown_kind, "kind", "bogus")
    with pytest.raises(ValueError, match="Unsupported bug case kind"):
        dispatcher.collect_evidence(cand, ReplayPlan(candidate_id=cand.id, cases=(unknown_kind,)), "base-sha")

    assert github.created_branches == []
    assert github.deleted_branches == []


def test_run_cycle_opens_pr_with_verdict_and_replay_summary_for_kept_candidate() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(
        repo="org/repo",
        description="Cache uv virtualenv across CI runs",
        skill="dep-cache",
        expected_savings_seconds=50.0,
        workflow_path=".github/workflows/ci.yml",
    )
    cand = Candidate(
        id="cand-pr-ok",
        hypothesis_id=hyp.id,
        branch="pc/uv-cache",
        summary="Enable uv cache in CI workflow",
        repo="org/repo",
        base_sha="base-sha-0",
        head_sha="cand-sha-0",
        files={".github/workflows/ci.yml": "name: Cached CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(kind="commit", commit_sha="deadbeef01", reason="regression commit"),
            BugCase(kind="mutation", mutated_files={"src/dead.py": "# noop\n"}, reason="dud mutant"),
            BugCase(kind="mutation", mutated_files={"src/auth.py": "return True\n"}, reason="auth bypass mutant"),
        ),
    )

    baseline_durations = (100.0, 98.0, 102.0, 99.0, 101.0)
    candidate_durations = (50.0, 49.0, 51.0, 48.0, 52.0)
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        run_scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=baseline_durations[pair_idx])]
        run_scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=candidate_durations[pair_idx])]
    # Run 11: commit replay fails -> caught.
    # Run 12: dud mutation baseline succeeds -> excluded (dud).
    # Run 13 & 14: valid mutation baseline fails, candidate fails -> caught.
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=18.0)]
    run_scripts[12] = [_make_run(12, conclusion="success", duration_seconds=12.0)]
    run_scripts[13] = [_make_run(13, conclusion="failure", duration_seconds=15.0)]
    run_scripts[14] = [_make_run(14, conclusion="failure", duration_seconds=11.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner, config=DispatcherConfig(timing_pairs=5))

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is True
    assert verdict.sla_met is True
    assert verdict.p_value is not None
    assert ledger.get(Verdict, verdict.id) == verdict
    assert len(github.pull_requests) == 1

    pr = github.pull_requests[0]
    assert pr["repo"] == "org/repo"
    assert pr["head"] == "pc/uv-cache"
    assert pr["base"] == "main"
    assert pr["title"] == "perf(ci): [dep-cache] Enable uv cache in CI workflow (2.00x faster)"

    body = pr["body"]
    assert f"| Speedup | {verdict.speedup:.2f}x |" in body
    assert f"| p-value | {verdict.p_value:.4f} |" in body
    assert "| Median duration | 50.0s |" in body
    assert "| p95 duration | 51.8s |" in body
    assert "| SLA status | Met |" in body
    assert "| Bugs caught | 2/2 |" in body
    assert "| Kind | Ref | Baseline Failed | Caught | Run |" in body
    assert "| commit | `deadbeef01` | Yes | Yes | [View run](https://github.com/org/repo/actions/runs/11) |" in body
    assert (
        "| mutation | `dud mutant` | No | Excluded (dud) | [View run](https://github.com/org/repo/actions/runs/12) |"
        in body
    )
    assert (
        "| mutation | `auth bypass mutant` | Yes | Yes | [View run](https://github.com/org/repo/actions/runs/14) |"
        in body
    )


def test_run_cycle_opens_pr_with_sla_not_met_and_custom_base_branch() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(
        repo="org/repo",
        description="Parallelize lint and unit tests",
        skill="parallelize-dag",
        expected_savings_seconds=100.0,
    )
    cand = Candidate(
        id="cand-pr-sla-miss",
        hypothesis_id=hyp.id,
        branch="pc/parallel-dag",
        summary="Run lint and tests in parallel",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Parallel CI\n"},
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-commit-1",))

    # 200s -> 100s is 2.00x faster (kept), but median 100s > 60s -> SLA Not met.
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        run_scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=200.0 + pair_idx)]
        run_scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=100.0 + pair_idx)]
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=20.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=runner,
        config=DispatcherConfig(timing_pairs=5, base_branch="develop"),
    )

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is True
    assert verdict.sla_met is False
    assert len(github.pull_requests) == 1
    pr = github.pull_requests[0]
    assert pr["repo"] == "org/repo"
    assert pr["head"] == "pc/parallel-dag"
    assert pr["base"] == "develop"
    assert "| SLA status | Not met |" in pr["body"]
    assert "| Median duration | 102.0s |" in pr["body"]


def test_run_cycle_does_not_open_pr_when_auto_open_pr_is_false() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(repo="org/repo", description="Cache deps", skill="dep-cache", expected_savings_seconds=50.0)
    cand = Candidate(
        id="cand-no-pr",
        hypothesis_id=hyp.id,
        branch="pc/no-pr",
        summary="Cache deps",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Cached CI\n"},
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-1",))

    run_scripts = _timing_run_scripts()
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=15.0)]
    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=runner,
        config=DispatcherConfig(timing_pairs=5, auto_open_pr=False),
    )

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is True
    assert ledger.get(Verdict, verdict.id) == verdict
    assert len(ledger.all(Evidence)) == 1
    assert github.pull_requests == []


def test_run_cycle_does_not_open_pr_when_bug_replay_fails() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(repo="org/repo", description="Shard tests", skill="shard-by-timing", expected_savings_seconds=60.0)
    cand = Candidate(
        id="cand-missed-bug",
        hypothesis_id=hyp.id,
        branch="pc/missed-bug",
        summary="Drop flaky test shard",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: Sharded CI\n"},
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-1",))

    run_scripts = _timing_run_scripts()
    # Replay concludes success -> bug was NOT caught.
    run_scripts[11] = [_make_run(11, conclusion="success", duration_seconds=15.0)]
    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner, config=DispatcherConfig(timing_pairs=5))

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason == "caught 0/1 bugs"
    assert ledger.get(Verdict, verdict.id) == verdict
    assert github.pull_requests == []


def test_run_cycle_does_not_open_pr_when_timings_are_noisy() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(repo="org/repo", description="Dedupe job", skill="dedupe-jobs", expected_savings_seconds=10.0)
    cand = Candidate(
        id="cand-noisy",
        hypothesis_id=hyp.id,
        branch="pc/noisy",
        summary="Minor job tweak",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("red-1",))

    noisy_base = (100.0, 95.0, 105.0, 98.0, 102.0)
    noisy_cand = (99.0, 104.0, 96.0, 101.0, 97.0)
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        run_scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=noisy_base[pair_idx])]
        run_scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=noisy_cand[pair_idx])]
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=15.0)]

    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
            "proof": lambda active, _t, _r: active.add(plan),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner, config=DispatcherConfig(timing_pairs=5))

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason == "speedup is within noise"
    assert ledger.get(Verdict, verdict.id) == verdict
    assert github.pull_requests == []


def test_run_cycle_does_not_open_pr_for_disallowed_candidate_path() -> None:
    ledger = InMemoryLedger()
    hyp = Hypothesis(repo="org/repo", description="Skip tests", skill="affected-tests", expected_savings_seconds=80.0)
    cand = Candidate(
        id="cand-bad-path",
        hypothesis_id=hyp.id,
        branch="pc/bad-path",
        summary="Modify application code",
        repo="org/repo",
        changed_files=("src/app.py",),
    )
    github = _FakeGitHubClient({"org/repo": "base-sha-0"})
    runner = _RecordingLaneRunner(
        {
            "analysis": lambda active, _t, _r: active.add(hyp),
            "optimizer": lambda active, _t, _r: active.add(cand),
        }
    )
    dispatcher = Dispatcher(ledger, github, lane_runner=runner, config=DispatcherConfig(auto_open_pr=True))

    verdict = dispatcher.run_cycle("org/repo")

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason == DISALLOWED_PATH_REASON
    assert ledger.get(Verdict, verdict.id) == verdict
    assert github.pull_requests == []


def test_format_pr_title_and_body_edge_cases() -> None:
    hyp = Hypothesis(
        repo="org/repo",
        description="First line description\nSecond line detail",
        skill="dep-cache",
        expected_savings_seconds=45.0,
    )
    cand_no_summary = Candidate(id="c1", hypothesis_id=hyp.id, branch="pc/c1", summary="   ", repo="org/repo")
    verdict_no_p = Verdict(
        candidate_id=cand_no_summary.id,
        keep=True,
        speedup=1.75,
        p_value=None,
        reason="1.75x faster",
        median_seconds=55.0,
        p95_seconds=70.0,
        sla_met=True,
    )
    assert format_pr_title(hyp, cand_no_summary, verdict_no_p) == (
        "perf(ci): [dep-cache] First line description (1.75x faster)"
    )

    hyp_empty_desc = Hypothesis(repo="org/repo", description="  ", skill="dedupe-jobs", expected_savings_seconds=30.0)
    assert format_pr_title(hyp_empty_desc, cand_no_summary, verdict_no_p) == (
        "perf(ci): [dedupe-jobs] dedupe-jobs (1.75x faster)"
    )

    empty_evidence = Evidence(
        candidate_id=cand_no_summary.id,
        baseline_seconds=(100.0,),
        candidate_seconds=(55.0,),
        bugs_total=0,
        bugs_caught=0,
        bug_outcomes=(),
    )
    empty_body = format_pr_body(hyp, cand_no_summary, empty_evidence, verdict_no_p)
    assert "| p-value | N/A |" in empty_body
    assert "_No bug replay outcomes recorded._" in empty_body

    missed_evidence = Evidence(
        candidate_id=cand_no_summary.id,
        baseline_seconds=(100.0,),
        candidate_seconds=(55.0,),
        bugs_total=1,
        bugs_caught=0,
        bug_outcomes=(
            {
                "kind": "mutation",
                "ref": "pipe|char\nnext line",
                "caught": False,
                "baseline_failed": True,
                "run_url": "",
            },
        ),
    )
    missed_body = format_pr_body(hyp, cand_no_summary, missed_evidence, verdict_no_p)
    assert r"| mutation | `pipe\|char next line` | Yes | No | - |" in missed_body

    inconclusive_evidence = Evidence(
        candidate_id=cand_no_summary.id,
        baseline_seconds=(100.0,),
        candidate_seconds=(55.0,),
        bugs_total=0,
        bugs_caught=0,
        bug_outcomes=(
            {
                "kind": "commit",
                "ref": "deadbeef",
                "caught": False,
                "baseline_failed": True,
                "inconclusive": True,
                "conflict_paths": [".github/workflows/ci.yml"],
                "run_url": "",
            },
        ),
    )
    inconclusive_body = format_pr_body(hyp, cand_no_summary, inconclusive_evidence, verdict_no_p)
    assert "| commit | `deadbeef` | Yes | Inconclusive (overlay conflict) | - |" in inconclusive_body


@dataclass
class _FakeToolStep:
    type: str
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] | None = None


@dataclass
class _FakeInteractionOutcome:
    id: str
    status: str | None
    steps: list[_FakeToolStep] | None = None
    continuation_token: str | None = None
    environment_id: str | None = None


def test_default_runner_profiles_ci_and_records_hypothesis_without_host_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(managed_agent, "POLL_INTERVAL_SECONDS", 0.0)
    workflow_yaml = (
        "name: CI\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"
    )
    sample_runs: list[dict[str, Any]] = [
        {
            "id": 1,
            "path": ".github/workflows/tests.yml",
            "event": "pull_request",
            "head_branch": "main",
            "head_sha": "a" * 40,
            "conclusion": "success",
            "run_attempt": 1,
            "run_started_at": "2026-11-01T00:00:00Z",
            "jobs": [
                {
                    "id": 10,
                    "name": "test",
                    "run_attempt": 1,
                    "conclusion": "success",
                    "created_at": "2026-11-01T00:00:00Z",
                    "started_at": "2026-11-01T00:00:00Z",
                    "completed_at": "2026-11-01T00:02:00Z",
                    "labels": [],
                    "steps": [
                        {
                            "number": 1,
                            "name": "Run pytest",
                            "conclusion": "success",
                            "started_at": "2026-11-01T00:00:00Z",
                            "completed_at": "2026-11-01T00:02:00Z",
                        }
                    ],
                }
            ],
        }
    ]
    monkeypatch.setattr(analysis_history, "fetch_history", lambda *_a, **_kw: sample_runs)
    monkeypatch.setattr(analysis_history, "fetch_workflow", lambda *_a, **_kw: workflow_yaml)
    blob = analysis_history.workflow_blob_sha(workflow_yaml)  # every run used this exact file
    monkeypatch.setattr(
        analysis_history, "fetch_workflow_blob_shas", lambda _r, _p, refs, **_k: dict.fromkeys(refs, blob)
    )

    class _FakeInteractions:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self._queue: list[_FakeInteractionOutcome] = [
                # Analysis turn 1: profile_ci without host checkout (workflow_content + GitHub fallback).
                _FakeInteractionOutcome(
                    id="int-a1",
                    status="requires_action",
                    steps=[
                        _FakeToolStep(
                            type="function_call",
                            id="call-p1",
                            name="profile_ci",
                            arguments={
                                "repo": "org/widget",
                                "workflow": "tests.yml",
                                "workflow_content": workflow_yaml,
                            },
                        ),
                        _FakeToolStep(
                            type="function_call",
                            id="call-p2",
                            name="profile_ci",
                            arguments={"repo": "org/widget", "workflow": "tests.yml", "ref": "base-sha-0"},
                        ),
                    ],
                ),
                # Analysis turn 2: records a hypothesis after profiling succeeds.
                _FakeInteractionOutcome(
                    id="int-a2",
                    status="requires_action",
                    steps=[
                        _FakeToolStep(
                            type="function_call",
                            id="call-h1",
                            name="record_hypothesis",
                            arguments={
                                "repo": "org/widget",
                                "description": "Shard pytest suite",
                                "skill": "shard-by-timing",
                                "expected_savings_seconds": 60.0,
                                "workflow_path": ".github/workflows/tests.yml",
                                "job": "test",
                                "base_sha": "base-sha-0",
                            },
                        )
                    ],
                ),
                _FakeInteractionOutcome(id="int-a3", status="completed", steps=[]),
                # Optimizer completes without writing a candidate (declines).
                _FakeInteractionOutcome(id="int-o1", status="completed", steps=[]),
            ]

        def create(self, **kwargs: object) -> _FakeInteractionOutcome:
            self.calls.append(kwargs)
            return self._queue.pop(0)

        def get(self, interaction_id: str, /) -> _FakeInteractionOutcome:
            msg = f"Unexpected get({interaction_id!r})"
            raise AssertionError(msg)

    class _FakeGeminiClient:
        def __init__(self) -> None:
            self.interactions = _FakeInteractions()

    fake_client = _FakeGeminiClient()
    monkeypatch.setattr(managed_agent, "_gemini_client", lambda: fake_client)

    ledger = InMemoryLedger()
    github = _FakeGitHubClient({"org/widget": "base-sha-0"})
    dispatcher = Dispatcher(ledger, github)

    assert dispatcher.run_cycle("org/widget") is None

    hyps = ledger.hypotheses_for_repo("org/widget")
    assert len(hyps) == 1
    assert hyps[0].skill == "shard-by-timing"
    profile_replies = fake_client.interactions.calls[1]["input"]
    assert isinstance(profile_replies, list)
    assert all(not reply.get("is_error") for reply in profile_replies)


def test_consecutive_cycles_skip_declined_hypothesis_but_retry_incomplete_optimizer() -> None:
    ledger = InMemoryLedger()
    h_top = Hypothesis(
        id="h-top",
        repo="org/repo",
        description="Inapplicable test selection",
        skill="affected-tests",
        expected_savings_seconds=120.0,
    )
    h_next = Hypothesis(
        id="h-next",
        repo="org/repo",
        description="Viable dependency cache",
        skill="dep-cache",
        expected_savings_seconds=60.0,
    )
    ledger.add(h_top)
    ledger.add(h_next)

    optimizer_outcomes: list[str | None] = ["failed", "completed", "completed"]
    seen_optimizer_hyps: list[str] = []

    class _ScriptedDeclineRunner:
        def __call__(
            self,
            lane: str,
            task: str,
            ledger: Ledger | None = None,
            *,
            repo: str | None = None,
        ) -> object:
            del repo
            assert ledger is not None
            if lane == "analysis":
                return _FakeInteractionOutcome(id="a", status="completed")
            if lane == "optimizer":
                ctx = _parse_task_context(task)
                hyp_id = str(ctx["hypothesis"]["id"])
                seen_optimizer_hyps.append(hyp_id)
                status = optimizer_outcomes.pop(0)
                if hyp_id == "h-next":
                    ledger.add(
                        Candidate(
                            id="cand-next",
                            hypothesis_id=hyp_id,
                            branch="pc/next",
                            summary="add cache",
                            repo="org/repo",
                            head_sha="head-next",
                            changed_files=(".github/workflows/ci.yml",),
                        )
                    )
                return _FakeInteractionOutcome(id="o", status=status)
            if lane == "proof":
                ctx = _parse_task_context(task)
                ledger.add(ReplayPlan(candidate_id=str(ctx["candidate"]["id"]), bug_shas=("red-1",)))
                return _FakeInteractionOutcome(id="p", status="completed")
            return None

    run_scripts = _timing_run_scripts()
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=12.0)]
    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(ledger, github, lane_runner=_ScriptedDeclineRunner())

    # Cycle 1: Optimizer fails -> returns None and does NOT mark h-top declined.
    assert dispatcher.run_cycle("org/repo") is None
    assert seen_optimizer_hyps == ["h-top"]

    # Cycle 2: Optimizer retries h-top and definitively declines (completed without candidate).
    assert dispatcher.run_cycle("org/repo") is None
    assert seen_optimizer_hyps == ["h-top", "h-top"]

    # Cycle 3: Selector moves past declined h-top and optimizes h-next!
    verdict = dispatcher.run_cycle("org/repo")
    assert verdict is not None
    assert verdict.candidate_id == "cand-next"
    assert verdict.keep is True
    assert seen_optimizer_hyps == ["h-top", "h-top", "h-next"]


def test_proof_no_replay_plan_records_unproven_verdict_and_incomplete_proof_resumes() -> None:
    # Part A: Proof definitively completes without a ReplayPlan -> terminal unproven Verdict.
    ledger = InMemoryLedger()
    h1 = Hypothesis(id="h1", repo="org/repo", description="first", skill="dep-cache", expected_savings_seconds=100.0)
    h2 = Hypothesis(id="h2", repo="org/repo", description="second", skill="dedupe-jobs", expected_savings_seconds=50.0)
    ledger.add(h1)
    ledger.add(h2)
    c1 = Candidate(
        id="c1",
        hypothesis_id=h1.id,
        branch="pc/c1",
        summary="first opt",
        repo="org/repo",
        head_sha="head-c1",
        changed_files=(".github/workflows/ci.yml",),
    )
    c2 = Candidate(
        id="c2",
        hypothesis_id=h2.id,
        branch="pc/c2",
        summary="second opt",
        repo="org/repo",
        head_sha="head-c2",
        changed_files=(".github/workflows/ci.yml",),
    )

    class _ProofEmptyRunner:
        def __call__(
            self,
            lane: str,
            task: str,
            ledger: Ledger | None = None,
            *,
            repo: str | None = None,
        ) -> object:
            del repo
            assert ledger is not None
            if lane == "optimizer":
                hyp_id = str(_parse_task_context(task)["hypothesis"]["id"])
                ledger.add(c1 if hyp_id == "h1" else c2)
            elif lane == "proof":
                cand_id = str(_parse_task_context(task)["candidate"]["id"])
                if cand_id == "c2":
                    ledger.add(ReplayPlan(candidate_id=cand_id, bug_shas=("red-1",)))
            return _FakeInteractionOutcome(id=lane, status="completed")

    run_scripts = _timing_run_scripts()
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=12.0)]
    github = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts)
    dispatcher = Dispatcher(ledger, github, lane_runner=_ProofEmptyRunner())

    v1 = dispatcher.run_cycle("org/repo")
    assert v1 is not None
    assert v1.candidate_id == "c1"
    assert v1.keep is False
    assert v1.reason == NO_REPLAY_PLAN_REASON
    attempts = ledger.prior_attempts("org/repo")
    assert next(a for a in attempts if a["candidate_id"] == "c1")["rated"] is True

    # Cycle 2 moves cleanly to h2 -> c2.
    v2 = dispatcher.run_cycle("org/repo")
    assert v2 is not None
    assert v2.candidate_id == "c2"
    assert v2.keep is True

    # Part B: Proof fails (incomplete) -> no verdict, candidate stays pending, next cycle resumes at Proof!
    ledger_b = InMemoryLedger()
    hb = Hypothesis(id="hb", repo="org/repo", description="cache", skill="dep-cache", expected_savings_seconds=80.0)
    ledger_b.add(hb)
    calls_b: list[str] = []
    proof_attempts = 0

    class _FlakyProofRunner:
        def __call__(
            self,
            lane: str,
            task: str,
            ledger: Ledger | None = None,
            *,
            repo: str | None = None,
        ) -> object:
            nonlocal proof_attempts
            del repo
            assert ledger is not None
            calls_b.append(lane)
            if lane == "optimizer":
                ledger.add(
                    Candidate(
                        id="cand-pending",
                        hypothesis_id=hb.id,
                        branch="pc/pending",
                        summary="add cache",
                        repo="org/repo",
                        files={".github/workflows/ci.yml": "name: Cached\n"},
                    )
                )
                return _FakeInteractionOutcome(id="o", status="completed")
            if lane == "proof":
                proof_attempts += 1
                if proof_attempts == 1:
                    return _FakeInteractionOutcome(id="p1", status="failed")
                cand_id = str(_parse_task_context(task)["candidate"]["id"])
                ledger.add(ReplayPlan(candidate_id=cand_id, bug_shas=("red-b",)))
                return _FakeInteractionOutcome(id="p2", status="completed")
            return _FakeInteractionOutcome(id="a", status="completed")

    run_scripts_b = _timing_run_scripts()
    run_scripts_b[11] = [_make_run(11, conclusion="failure", duration_seconds=12.0)]
    github_b = _FakeGitHubClient({"org/repo": "base-sha-0"}, run_scripts=run_scripts_b, require_known_branch=True)
    dispatcher_b = Dispatcher(ledger_b, github_b, lane_runner=_FlakyProofRunner())

    assert dispatcher_b.run_cycle("org/repo") is None
    assert calls_b == ["analysis", "optimizer", "proof"]
    assert ledger_b.all(Verdict) == []
    # Candidate branch was already published on Cycle 1.
    assert ("org/repo", "pc/pending") in github_b.branches

    # Cycle 2 resumes pending candidate directly at Proof without re-running analysis/optimizer or recreating branch!
    resumed_verdict = dispatcher_b.run_cycle("org/repo")
    assert resumed_verdict is not None
    assert resumed_verdict.candidate_id == "cand-pending"
    assert resumed_verdict.keep is True
    assert calls_b == ["analysis", "optimizer", "proof", "proof"]


def test_second_cycle_binds_candidate_base_sha_after_default_branch_advances() -> None:
    managed_agent.build_request("optimizer", "")
    ledger = InMemoryLedger()
    h1 = Hypothesis(
        id="h1",
        repo="org/repo",
        description="first",
        skill="dep-cache",
        expected_savings_seconds=100.0,
        base_sha="sha-commit-A",
    )
    h2 = Hypothesis(
        id="h2",
        repo="org/repo",
        description="second",
        skill="dedupe-jobs",
        expected_savings_seconds=60.0,
        base_sha="sha-commit-A",
    )
    ledger.add(h1)
    ledger.add(h2)

    base_wf = (
        "name: CI\non:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"
    )
    opt_wf = (
        "name: CI\non:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: pytest -q\n"
    )
    # Cycle 1 fails timing -> rejected so default branch is consulted again on Cycle 2.
    # Cycle 2 succeeds after default branch moved from sha-commit-A to sha-commit-B.
    run_scripts: dict[int, list[WorkflowRunResult]] = {
        1: [_make_run(1, duration_seconds=100.0)],
        2: [_make_run(2, conclusion="failure", duration_seconds=5.0)],
        3: [_make_run(3, duration_seconds=100.0)],
        4: [_make_run(4, duration_seconds=100.0)],
        5: [_make_run(5, duration_seconds=100.0)],
        6: [_make_run(6, duration_seconds=100.0)],
        7: [_make_run(7, conclusion="failure", duration_seconds=10.0)],
    }
    for pair_idx in range(5):
        run_scripts[8 + pair_idx * 2] = [_make_run(8 + pair_idx * 2, duration_seconds=100.0 + pair_idx)]
        run_scripts[9 + pair_idx * 2] = [_make_run(9 + pair_idx * 2, duration_seconds=45.0 + pair_idx)]
    run_scripts[18] = [_make_run(18, conclusion="failure", duration_seconds=12.0)]

    github = _FakeGitHubClient({"org/repo": "sha-commit-A"}, run_scripts=run_scripts, require_known_branch=True)

    def on_optimizer(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        ctx = _parse_task_context(task)
        hyp_id = str(ctx["hypothesis"]["id"])
        # Simulate Optimizer calling report_candidate WITHOUT explicitly passing base_sha (stale hypothesis.base_sha).
        tools.call(
            "report_candidate",
            active_ledger,
            {
                "hypothesis_id": hyp_id,
                "branch": f"pc/{hyp_id}-opt",
                "summary": f"optimize {hyp_id}",
                "files": {".github/workflows/ci.yml": opt_wf},
                "original_files": {".github/workflows/ci.yml": base_wf},
            },
        )

    def on_proof(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        ctx = _parse_task_context(task)
        active_ledger.add(ReplayPlan(candidate_id=str(ctx["candidate"]["id"]), bug_shas=("red-1",)))

    runner = _RecordingLaneRunner({"optimizer": on_optimizer, "proof": on_proof})
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    v1 = dispatcher.run_cycle("org/repo")
    assert v1 is not None
    assert v1.keep is False

    # Default branch advances to sha-commit-B before Cycle 2.
    github._default_shas["org/repo"] = "sha-commit-B"  # noqa: SLF001

    v2 = dispatcher.run_cycle("org/repo")
    assert v2 is not None
    assert v2.keep is True
    c2 = ledger.get(Candidate, v2.candidate_id)
    assert c2.hypothesis_id == "h2"
    # Candidate is bound to the resolved cycle base B, NOT stale hypothesis.base_sha A!
    assert c2.base_sha == "sha-commit-B"
    assert c2.head_sha.startswith("commit-")
    assert ("org/repo", "pc/h2-opt", "sha-commit-B") in github.created_branches
    assert ("org/repo", f"pc/baseline-{c2.id}", "sha-commit-B") in github.created_branches


def test_run_cycle_publishes_candidate_enforces_named_refs_and_compounds() -> None:
    managed_agent.build_request("optimizer", "")
    ledger = InMemoryLedger()
    h1 = Hypothesis(id="h1", repo="org/repo", description="first", skill="dep-cache", expected_savings_seconds=100.0)
    h2 = Hypothesis(id="h2", repo="org/repo", description="second", skill="dedupe-jobs", expected_savings_seconds=60.0)
    ledger.add(h1)
    ledger.add(h2)

    base_wf = (
        "name: CI\non:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"
    )
    opt_wf_1 = base_wf.replace("pytest", "pytest -q")
    opt_wf_2 = opt_wf_1.replace("pytest -q", "pytest -q -n auto")

    # Cycle 1: 200s -> 100s (kept, SLA not met -> compounds). Cycle 2: 100s -> 45s (kept, SLA met).
    run_scripts: dict[int, list[WorkflowRunResult]] = {}
    for pair_idx in range(5):
        run_scripts[pair_idx * 2 + 1] = [_make_run(pair_idx * 2 + 1, duration_seconds=200.0 + pair_idx)]
        run_scripts[pair_idx * 2 + 2] = [_make_run(pair_idx * 2 + 2, duration_seconds=100.0 + pair_idx)]
    run_scripts[11] = [_make_run(11, conclusion="failure", duration_seconds=15.0)]
    for pair_idx in range(5):
        run_scripts[12 + pair_idx * 2] = [_make_run(12 + pair_idx * 2, duration_seconds=100.0 + pair_idx)]
        run_scripts[13 + pair_idx * 2] = [_make_run(13 + pair_idx * 2, duration_seconds=45.0 + pair_idx)]
    run_scripts[22] = [_make_run(22, conclusion="failure", duration_seconds=12.0)]

    # Strict client: rejects ANY dispatch_workflow on a raw SHA or unpublished branch!
    github = _FakeGitHubClient(
        {"org/repo": "0" * 40},
        run_scripts=run_scripts,
        require_known_branch=True,
    )

    def on_optimizer(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        ctx = _parse_task_context(task)
        hyp_id = str(ctx["hypothesis"]["id"])
        before, after = (base_wf, opt_wf_1) if hyp_id == "h1" else (opt_wf_1, opt_wf_2)
        tools.call(
            "report_candidate",
            active_ledger,
            {
                "hypothesis_id": hyp_id,
                "branch": f"pc/{hyp_id}",
                "summary": f"apply {hyp_id}",
                "files": {".github/workflows/ci.yml": after},
                "original_files": {".github/workflows/ci.yml": before},
                "base_sha": str(ctx["base_sha"]),
            },
        )

    def on_proof(active_ledger: Ledger, task: str, _repo: str | None) -> None:
        ctx = _parse_task_context(task)
        active_ledger.add(ReplayPlan(candidate_id=str(ctx["candidate"]["id"]), bug_shas=("1" * 40,)))

    runner = _RecordingLaneRunner({"optimizer": on_optimizer, "proof": on_proof})
    dispatcher = Dispatcher(ledger, github, lane_runner=runner)

    verdicts = dispatcher.run_until_sla("org/repo")

    assert len(verdicts) == 2
    assert [v.keep for v in verdicts] == [True, True]
    assert [v.sla_met for v in verdicts] == [False, True]
    c1 = ledger.get(Candidate, verdicts[0].candidate_id)
    c2 = ledger.get(Candidate, verdicts[1].candidate_id)
    assert c1.head_sha == "commit-1"
    assert c2.base_sha == c1.head_sha
    assert c2.head_sha == "commit-3"
    # Temporary baseline and replay branches are all cleaned up; published candidate branches remain.
    assert set(github.branches) == {("org/repo", "pc/h1"), ("org/repo", "pc/h2")}

    # Verify cleanup when commit_files fails while publishing a candidate.
    fail_ledger = InMemoryLedger()
    fail_h = Hypothesis(repo="org/repo", description="fail", skill="dep-cache", expected_savings_seconds=50.0)
    fail_ledger.add(fail_h)
    fail_github = _FakeGitHubClient(
        {"org/repo": "a" * 40},
        fail_on={"commit_files": RuntimeError("commit exploded")},
    )
    fail_runner = _RecordingLaneRunner(
        {
            "optimizer": lambda active, _t, _r: active.add(
                Candidate(
                    hypothesis_id=fail_h.id,
                    branch="pc/fail-pub",
                    summary="x",
                    repo="org/repo",
                    files={".github/workflows/ci.yml": opt_wf_1},
                )
            )
        }
    )
    with pytest.raises(RuntimeError, match="commit exploded"):
        Dispatcher(fail_ledger, fail_github, lane_runner=fail_runner).run_cycle("org/repo")
    assert fail_github.branches == {}


def test_baseline_timing_branch_is_cleaned_up_on_dispatch_error() -> None:
    ledger = InMemoryLedger()
    github = _FakeGitHubClient(
        {"org/repo": "a" * 40},
        fail_on={"dispatch_workflow": RuntimeError("actions unavailable")},
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=2),
    )
    cand = Candidate(id="cand-clean", hypothesis_id="h1", branch="pc/clean", summary="s", repo="org/repo")
    with pytest.raises(RuntimeError, match="actions unavailable"):
        dispatcher.collect_evidence(cand, ReplayPlan(candidate_id=cand.id), "a" * 40, workflow="ci.yml")
    assert github.created_branches == [("org/repo", "pc/baseline-cand-clean", "a" * 40)]
    assert github.deleted_branches == [("org/repo", "pc/baseline-cand-clean")]
    assert github.branches == {}


def test_three_way_overlay_preserves_separate_line_edits_and_marks_conflicts_inconclusive() -> None:
    base_pyproject = "[tool.ruff]\nline-length = 120\n\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    cand_pyproject = "[tool.ruff]\nline-length = 120\n\n[tool.pytest.ini_options]\naddopts = '-q -n auto'\n"
    hist_nonconflict = "[tool.ruff]\nline-length = 88\n\n[tool.pytest.ini_options]\naddopts = '-q'\n"
    hist_conflict = "[tool.ruff]\nline-length = 120\n\n[tool.pytest.ini_options]\naddopts = '--strict-fail'\n"

    # 1. Commit replays: one non-conflicting shared file + one conflicting shared file.
    github = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, conclusion="failure", duration_seconds=12.0)]},
        file_contents={
            ("org/repo", "base-sha", "pyproject.toml"): base_pyproject,
            ("org/repo", "commit-clean", "pyproject.toml"): hist_nonconflict,
            ("org/repo", "commit-conflict", "pyproject.toml"): hist_conflict,
        },
    )
    dispatcher = Dispatcher(
        InMemoryLedger(),
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-3way",
        hypothesis_id="h1",
        branch="pc/3way",
        summary="add xdist",
        repo="org/repo",
        base_sha="base-sha",
        files={"pyproject.toml": cand_pyproject},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(
            BugCase(kind="commit", commit_sha="commit-clean", reason="separate line bug"),
            BugCase(kind="commit", commit_sha="commit-conflict", reason="same line bug"),
        ),
    )

    ev = dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")
    assert ev.bugs_total == 1
    assert ev.bugs_caught == 1
    assert len(github.commits) == 1
    assert github.commits[0]["files"] == {
        "pyproject.toml": "[tool.ruff]\nline-length = 88\n\n[tool.pytest.ini_options]\naddopts = '-q -n auto'\n"
    }
    assert ev.bug_outcomes[1]["inconclusive"] is True
    assert ev.bug_outcomes[1]["conflict_paths"] == ["pyproject.toml"]

    # 2. Mutation replay with conflicting shared-file edit: baseline runs, candidate replay skipped & inconclusive.
    github_mut = _FakeGitHubClient(
        {"org/repo": "base-sha"},
        run_scripts={1: [_make_run(1, conclusion="failure", duration_seconds=11.0)]},
        file_contents={("org/repo", "base-sha", "pyproject.toml"): base_pyproject},
    )
    dispatcher_mut = Dispatcher(
        InMemoryLedger(),
        github_mut,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    mut_plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={"pyproject.toml": hist_conflict}, reason="conflict mut"),),
    )
    mut_ev = dispatcher_mut.collect_evidence(cand, mut_plan, "base-sha", workflow="ci.yml")
    assert mut_ev.bugs_total == 0
    assert mut_ev.bugs_caught == 0
    assert mut_ev.bug_outcomes[0]["inconclusive"] is True
    assert mut_ev.bug_outcomes[0]["conflict_paths"] == ["pyproject.toml"]
    assert github_mut.created_branches == [("org/repo", "pc/replay-cand-3way-0-base", "base-sha")]
    assert rate(replace(mut_ev, baseline_seconds=(100.0,) * 5, candidate_seconds=(50.0,) * 5)).keep is False


@pytest.mark.parametrize(
    "bad_mutation_path",
    [
        ".github/workflows/ci.yml",
        ".github/actions/setup/action.yml",
        ".GITHUB/workflows/ci.yml",
        "../escape.py",
        "/etc/passwd",
    ],
)
def test_collect_evidence_rejects_proof_mutation_targeting_disallowed_paths(bad_mutation_path: str) -> None:
    github = _FakeGitHubClient({"org/repo": "base-sha"})
    dispatcher = Dispatcher(
        InMemoryLedger(),
        github,
        lane_runner=_RecordingLaneRunner(),
        config=DispatcherConfig(timing_pairs=0),
    )
    cand = Candidate(
        id="cand-sec",
        hypothesis_id="h1",
        branch="pc/sec",
        summary="safe",
        repo="org/repo",
        files={".github/workflows/ci.yml": "name: CI\n"},
    )
    plan = ReplayPlan(
        candidate_id=cand.id,
        cases=(BugCase(kind="mutation", mutated_files={bad_mutation_path: "name: Pwned\n"}, reason="evil"),),
    )

    with pytest.raises(ValueError, match="Disallowed mutation path"):
        dispatcher.collect_evidence(cand, plan, "base-sha", workflow="ci.yml")

    assert github.created_branches == []
    assert github.dispatches == []
