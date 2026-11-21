import json
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, fields
from typing import Any, override

import pytest

from pit_crew import managed_agent
from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.dispatcher import DISALLOWED_PATH_REASON, Dispatcher, DispatcherConfig, LaneRunner, next_lane
from pit_crew.github import GitHubClient, WorkflowRunResult
from pit_crew.ledger import InMemoryLedger, Ledger


class _FakeGitHubClient:
    """Offline fake implementing ``GitHubClient`` for Dispatcher unit tests."""

    def __init__(self, default_shas: dict[str, str]) -> None:
        self._default_shas = dict(default_shas)
        self.default_branch_lookups: list[str] = []
        self.dispatches: list[tuple[str, str, str]] = []
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
    assert github.dispatches == []


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
