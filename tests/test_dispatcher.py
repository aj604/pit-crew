import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, fields, replace
from typing import Any, override

import pytest

from pit_crew import managed_agent
from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.dispatcher import DISALLOWED_PATH_REASON, Dispatcher, DispatcherConfig, LaneRunner, next_lane
from pit_crew.github import GitHubClient, WorkflowRunResult
from pit_crew.ledger import InMemoryLedger, Ledger
from pit_crew.rater import rate


class _FakeGitHubClient:
    """Offline fake implementing ``GitHubClient`` for Dispatcher unit tests."""

    def __init__(
        self,
        default_shas: dict[str, str],
        run_scripts: dict[int, list[WorkflowRunResult]] | None = None,
    ) -> None:
        self._default_shas = dict(default_shas)
        self._run_scripts: dict[int, list[WorkflowRunResult]] = {
            run_id: list(results) for run_id, results in (run_scripts or {}).items()
        }
        self.default_branch_lookups: list[str] = []
        self.dispatches: list[tuple[str, str, str]] = []
        self.poll_calls: list[tuple[str, int]] = []
        self.branches: dict[tuple[str, str], str] = {}
        self.commits: list[dict[str, object]] = []
        self.pull_requests: list[dict[str, str]] = []

    def get_default_branch_sha(self, repo: str) -> str:
        self.default_branch_lookups.append(repo)
        return self._default_shas[repo]

    def create_branch(self, repo: str, branch: str, base_sha: str) -> str:
        self.branches[(repo, branch)] = base_sha
        return base_sha

    def delete_branch(self, repo: str, branch: str) -> None:
        del self.branches[(repo, branch)]

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        head_sha = f"commit-{len(self.commits) + 1}"
        self.branches[(repo, branch)] = head_sha
        self.commits.append({"repo": repo, "branch": branch, "base_sha": base_sha, "files": files, "message": message})
        return head_sha

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        self.dispatches.append((repo, workflow, ref))
        return len(self.dispatches)

    def get_workflow_run(self, repo: str, run_id: int) -> WorkflowRunResult:
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
    ]
    assert cfg.timing_pairs == 5
    assert cfg.max_compounding_cycles == 5
    assert cfg.workflow_file == "ci.yml"
    assert cfg.run_timeout_seconds == 900.0
    assert cfg.poll_interval_seconds == 5.0
    assert cfg.auto_open_pr is True

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
    assert (
        github.dispatches
        == [
            ("org/repo", "tests.yml", "compounded-sha-1"),
            ("org/repo", "tests.yml", "pc/shard-tests"),
        ]
        * 5
    )


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
    assert dispatcher.evidence_calls == [(cand, plan, "base-sha-0", "custom.yml")]
    assert (
        github.dispatches
        == [
            ("org/repo", "custom.yml", "base-sha-0"),
            ("org/repo", "custom.yml", "pc/cache"),
        ]
        * 5
    )


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
    plan = ReplayPlan(candidate_id=cand.id, bug_shas=("bug-1",))

    evidence = dispatcher.collect_evidence(cand, plan, "base-sha-1", workflow="ci.yml")

    assert (
        github.dispatches
        == [
            ("org/repo", "ci.yml", "base-sha-1"),
            ("org/repo", "ci.yml", "pc/cache"),
        ]
        * 5
    )
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
            ("org/repo", "bench.yml", "base-sha"),
            ("org/repo", "bench.yml", "cand-sha-99"),
        ]
        * 2
    )

    # Candidate without a ledger hypothesis falls back to config.workflow_file; explicit path normalizes to basename.
    github.dispatches.clear()
    orphan = Candidate(hypothesis_id="missing-hyp", branch="pc/orphan", summary="orphan", repo="org/repo")
    dispatcher.collect_evidence(orphan, ReplayPlan(candidate_id=orphan.id), "base-sha")
    assert (
        github.dispatches
        == [
            ("org/repo", "fallback.yml", "base-sha"),
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
            ("org/repo", "custom.yml", "base-sha"),
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
        ("org/repo", "ci.yml", "base-sha"),
        ("org/repo", "ci.yml", "pc/broken"),
        ("org/repo", "ci.yml", "base-sha"),
        ("org/repo", "ci.yml", "pc/broken"),
        ("org/repo", "ci.yml", "base-sha"),
        ("org/repo", "ci.yml", "base-sha"),
        ("org/repo", "ci.yml", "base-sha"),
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
        ("org/repo", "ci.yml", "base-sha"),
        ("org/repo", "ci.yml", "pc/fast"),
        ("org/repo", "ci.yml", "base-sha"),
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
        ("org/repo", "ci.yml", "base-sha"),
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
    assert verdict.keep is False
    assert verdict.median_seconds == 50.0
    assert verdict.p95_seconds == pytest.approx(51.8)
    assert verdict.sla_met is True
    assert verdict.reason == "no bugs replayed, so safety is unproven"

    stored_evidence = ledger.all(Evidence)
    assert len(stored_evidence) == 1
    ev = stored_evidence[0]
    assert ledger.get(Evidence, ev.id) == ev
    assert ev.candidate_id == cand.id
    assert ev.baseline_seconds == baseline_durations
    assert ev.candidate_seconds == candidate_durations
    assert ev.run_urls == tuple(f"https://github.com/org/repo/actions/runs/{i}" for i in range(1, 11))
    assert clock.sleeps == [2.0, 2.0]


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
    assert len(stored_evidence[0].run_urls) == 6
