import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan
from pit_crew.dispatcher import DISALLOWED_PATH_REASON, Dispatcher, DispatcherConfig, LaneRunner
from pit_crew.github import GitHubClient, WorkflowRunResult
from pit_crew.ledger import InMemoryLedger, Ledger, SqliteLedger


class _FakeGitHubClient:
    """Minimal offline ``GitHubClient`` for compounding loop tests."""

    def __init__(self, default_shas: dict[str, str]) -> None:
        self._default_shas = dict(default_shas)
        self.default_branch_lookups: list[str] = []
        self.pull_requests: list[dict[str, str]] = []

    def get_default_branch_sha(self, repo: str) -> str:
        self.default_branch_lookups.append(repo)
        return self._default_shas[repo]

    def create_branch(self, repo: str, branch: str, base_sha: str) -> str:
        del repo, branch
        return base_sha

    def delete_branch(self, repo: str, branch: str) -> None:
        del repo, branch

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        del repo, branch, base_sha, files, message
        return "unused-commit-sha"

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        del repo, workflow, ref
        return 1

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
class _EvidenceSpec:
    baseline_seconds: tuple[float, ...]
    candidate_seconds: tuple[float, ...]
    bugs_total: int = 1
    bugs_caught: int = 1


@dataclass(frozen=True)
class _LaneCall:
    lane: str
    context: dict[str, Any]
    repo: str | None


def _parse_task_context(task: str) -> dict[str, Any]:
    _, raw_json = task.split("Context:\n", maxsplit=1)
    parsed = json.loads(raw_json)
    assert isinstance(parsed, dict)
    return parsed


class _ScriptedLaneRunner:
    """Offline fake ``LaneRunner`` recording lane contexts and emitting deterministic records."""

    def __init__(
        self,
        *,
        on_analysis: Callable[[Ledger, dict[str, Any]], None] | None = None,
        candidate_files_by_hypothesis: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._on_analysis = on_analysis
        self._candidate_files_by_hypothesis = dict(candidate_files_by_hypothesis or {})
        self.calls: list[_LaneCall] = []

    def __call__(
        self,
        lane: str,
        task: str,
        ledger: Ledger | None = None,
        *,
        repo: str | None = None,
    ) -> None:
        context = _parse_task_context(task)
        self.calls.append(_LaneCall(lane=lane, context=context, repo=repo))
        if ledger is None:
            return
        if lane == "analysis":
            if self._on_analysis is not None:
                self._on_analysis(ledger, context)
            return
        if lane == "optimizer":
            hyp_id = str(context["hypothesis"]["id"])
            base_sha = str(context["base_sha"])
            changed_files = self._candidate_files_by_hypothesis.get(hyp_id, (".github/workflows/ci.yml",))
            ledger.add(
                Candidate(
                    id=f"cand-{hyp_id}",
                    hypothesis_id=hyp_id,
                    repo=str(context["repo"]),
                    branch=f"pit-crew/{hyp_id}",
                    base_sha=base_sha,
                    head_sha=f"head-{hyp_id}",
                    summary=f"Apply {hyp_id}",
                    changed_files=changed_files,
                )
            )
            return
        if lane == "proof":
            cand_id = str(context["candidate"]["id"])
            ledger.add(ReplayPlan(id=f"plan-{cand_id}", candidate_id=cand_id, bug_shas=("bug-sha-1",)))


class _ControlledEvidenceDispatcher(Dispatcher):
    """Dispatcher subclass supplying controlled Evidence so real ``run_cycle`` and ``rate`` execute end-to-end."""

    def __init__(
        self,
        ledger: Ledger,
        github: GitHubClient,
        lane_runner: LaneRunner,
        evidence_by_candidate: dict[str, _EvidenceSpec],
        config: DispatcherConfig | None = None,
    ) -> None:
        super().__init__(ledger, github, lane_runner=lane_runner, config=config)
        self._evidence_by_candidate = dict(evidence_by_candidate)
        self.evidence_base_shas: list[tuple[str, str]] = []

    @override
    def collect_evidence(
        self,
        candidate: Candidate,
        replay_plan: ReplayPlan,
        base_sha: str,
        *,
        workflow: str | None = None,
    ) -> Evidence:
        del replay_plan, workflow
        self.evidence_base_shas.append((candidate.id, base_sha))
        spec = self._evidence_by_candidate[candidate.id]
        return Evidence(
            candidate_id=candidate.id,
            baseline_seconds=spec.baseline_seconds,
            candidate_seconds=spec.candidate_seconds,
            bugs_total=spec.bugs_total,
            bugs_caught=spec.bugs_caught,
        )


def _seed_hypotheses(ledger: Ledger, repo: str, specs: list[tuple[str, float]]) -> None:
    skills = ("dep-cache", "parallelize-dag", "shard-by-timing", "dedupe-jobs", "affected-tests")
    for idx, (hyp_id, savings) in enumerate(specs):
        ledger.add(
            Hypothesis(
                id=hyp_id,
                repo=repo,
                description=f"Optimization {hyp_id}",
                skill=skills[idx % len(skills)],
                expected_savings_seconds=savings,
            )
        )


def test_run_until_sla_compounds_across_kept_and_rejected_cycles_then_stops_on_sla() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 70.0), ("h3", 50.0), ("h4", 20.0)])

    github = _FakeGitHubClient({repo: "sha-default-0"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            # Cycle 1: kept (1.67x faster), but median 120s > 60s -> SLA not yet met.
            "cand-h1": _EvidenceSpec(
                baseline_seconds=(200.0, 202.0, 198.0, 201.0, 199.0),
                candidate_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
            ),
            # Cycle 2: rejected (within noise / < 1.05x) -> base_sha must NOT advance to head-h2.
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
                candidate_seconds=(119.0, 121.0, 120.0, 121.0, 119.0),
            ),
            # Cycle 3: kept and meets 60s median / 120s p95 SLA -> must stop before h4.
            "cand-h3": _EvidenceSpec(
                baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
                candidate_seconds=(45.0, 46.0, 44.0, 47.0, 45.0),
            ),
            "cand-h4": _EvidenceSpec(
                baseline_seconds=(45.0, 46.0, 44.0, 47.0, 45.0),
                candidate_seconds=(30.0, 31.0, 29.0, 32.0, 30.0),
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert [v.candidate_id for v in verdicts] == ["cand-h1", "cand-h2", "cand-h3"]
    assert [v.keep for v in verdicts] == [True, False, True]
    assert [v.sla_met for v in verdicts] == [False, False, True]

    # base_sha advances after kept cycle 1, holds after rejected cycle 2, and never runs cycle 4.
    analysis_bases = [call.context["base_sha"] for call in runner.calls if call.lane == "analysis"]
    optimizer_bases = [call.context["base_sha"] for call in runner.calls if call.lane == "optimizer"]
    proof_bases = [call.context["base_sha"] for call in runner.calls if call.lane == "proof"]
    assert analysis_bases == ["sha-default-0", "head-h1", "head-h1"]
    assert optimizer_bases == ["sha-default-0", "head-h1", "head-h1"]
    assert proof_bases == ["sha-default-0", "head-h1", "head-h1"]
    assert dispatcher.evidence_base_shas == [
        ("cand-h1", "sha-default-0"),
        ("cand-h2", "head-h1"),
        ("cand-h3", "head-h1"),
    ]

    # Default branch SHA is only queried before the first kept candidate exists.
    assert github.default_branch_lookups == [repo]
    latest_kept = ledger.latest_kept_candidate(repo)
    assert latest_kept is not None
    assert latest_kept.head_sha == "head-h3"
    assert [pr["head"] for pr in github.pull_requests] == ["pit-crew/h1", "pit-crew/h3"]


def test_run_until_sla_advances_base_sha_across_consecutive_kept_cycles_with_sqlite(tmp_path: Path) -> None:
    repo = "org/repo"
    ledger = SqliteLedger(tmp_path / "ledger.db")
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 60.0), ("h3", 40.0)])

    github = _FakeGitHubClient({repo: "sha-main"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            "cand-h1": _EvidenceSpec(
                baseline_seconds=(240.0, 241.0, 239.0, 242.0, 238.0),
                candidate_seconds=(160.0, 161.0, 159.0, 162.0, 158.0),
            ),
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(160.0, 161.0, 159.0, 162.0, 158.0),
                candidate_seconds=(95.0, 96.0, 94.0, 97.0, 95.0),
            ),
            "cand-h3": _EvidenceSpec(
                baseline_seconds=(95.0, 96.0, 94.0, 97.0, 95.0),
                candidate_seconds=(50.0, 51.0, 49.0, 52.0, 50.0),
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert [v.candidate_id for v in verdicts] == ["cand-h1", "cand-h2", "cand-h3"]
    assert all(v.keep for v in verdicts)
    assert [v.sla_met for v in verdicts] == [False, False, True]
    assert dispatcher.evidence_base_shas == [
        ("cand-h1", "sha-main"),
        ("cand-h2", "head-h1"),
        ("cand-h3", "head-h2"),
    ]


def test_run_until_sla_does_not_stop_on_rejected_candidate_with_sla_met_timings() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 60.0), ("h3", 30.0)])

    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            # Fast enough for sla_met=True, but missed a replayed bug -> keep=False!
            "cand-h1": _EvidenceSpec(
                baseline_seconds=(180.0, 181.0, 179.0, 182.0, 178.0),
                candidate_seconds=(35.0, 36.0, 34.0, 37.0, 35.0),
                bugs_total=1,
                bugs_caught=0,
            ),
            # Valid kept candidate that catches all bugs and meets SLA -> stops before h3.
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(180.0, 181.0, 179.0, 182.0, 178.0),
                candidate_seconds=(52.0, 53.0, 51.0, 54.0, 52.0),
                bugs_total=1,
                bugs_caught=1,
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert [v.candidate_id for v in verdicts] == ["cand-h1", "cand-h2"]
    assert verdicts[0].keep is False
    assert verdicts[0].sla_met is True
    assert verdicts[1].keep is True
    assert verdicts[1].sla_met is True
    assert dispatcher.evidence_base_shas == [
        ("cand-h1", "sha-default"),
        ("cand-h2", "sha-default"),
    ]


def test_run_until_sla_stops_immediately_on_cycle_one_when_sla_met() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 40.0)])

    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            "cand-h1": _EvidenceSpec(
                baseline_seconds=(150.0, 151.0, 149.0, 152.0, 148.0),
                candidate_seconds=(48.0, 49.0, 47.0, 50.0, 48.0),
            ),
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(48.0, 49.0, 47.0, 50.0, 48.0),
                candidate_seconds=(30.0, 31.0, 29.0, 32.0, 30.0),
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert len(verdicts) == 1
    assert verdicts[0].candidate_id == "cand-h1"
    assert verdicts[0].keep is True
    assert verdicts[0].sla_met is True
    assert [call.lane for call in runner.calls] == ["analysis", "optimizer", "proof"]


def test_run_until_sla_stops_immediately_when_first_cycle_has_no_hypotheses() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={},
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert verdicts == []
    assert [call.lane for call in runner.calls] == ["analysis"]


def test_run_until_sla_stops_when_hypotheses_exhaust_mid_loop() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 60.0)])

    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            "cand-h1": _EvidenceSpec(
                baseline_seconds=(220.0, 221.0, 219.0, 222.0, 218.0),
                candidate_seconds=(150.0, 151.0, 149.0, 152.0, 148.0),
            ),
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(150.0, 151.0, 149.0, 152.0, 148.0),
                candidate_seconds=(100.0, 101.0, 99.0, 102.0, 98.0),
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert [v.candidate_id for v in verdicts] == ["cand-h1", "cand-h2"]
    assert all(v.keep and not v.sla_met for v in verdicts)
    # Cycle 3 runs analysis, finds no unattempted hypothesis, returns None, and stops (no cycle 4 or 5).
    analysis_calls = [call for call in runner.calls if call.lane == "analysis"]
    assert len(analysis_calls) == 3
    assert [call.context["base_sha"] for call in analysis_calls] == ["sha-default", "head-h1", "head-h2"]


def test_run_until_sla_enforces_max_compounding_cycles_cap() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 70.0), ("h3", 50.0), ("h4", 30.0)])

    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            "cand-h1": _EvidenceSpec(
                baseline_seconds=(240.0, 241.0, 239.0, 242.0, 238.0),
                candidate_seconds=(180.0, 181.0, 179.0, 182.0, 178.0),
            ),
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(180.0, 181.0, 179.0, 182.0, 178.0),
                candidate_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
            ),
            "cand-h3": _EvidenceSpec(
                baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
                candidate_seconds=(50.0, 51.0, 49.0, 52.0, 50.0),
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=2),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert [v.candidate_id for v in verdicts] == ["cand-h1", "cand-h2"]
    assert all(v.keep and not v.sla_met for v in verdicts)
    assert len([call for call in runner.calls if call.lane == "analysis"]) == 2
    assert dispatcher.evidence_base_shas == [
        ("cand-h1", "sha-default"),
        ("cand-h2", "head-h1"),
    ]


def test_run_until_sla_with_zero_max_compounding_cycles_runs_nothing() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0)])

    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner()
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={},
        config=DispatcherConfig(max_compounding_cycles=0),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert verdicts == []
    assert runner.calls == []
    assert github.default_branch_lookups == []


def test_run_until_sla_holds_base_sha_after_disallowed_path_rejection() -> None:
    repo = "org/repo"
    ledger = InMemoryLedger()
    _seed_hypotheses(ledger, repo, [("h1", 90.0), ("h2", 60.0)])

    github = _FakeGitHubClient({repo: "sha-default"})
    runner = _ScriptedLaneRunner(candidate_files_by_hypothesis={"h1": ("src/app.py",)})
    dispatcher = _ControlledEvidenceDispatcher(
        ledger=ledger,
        github=github,
        lane_runner=runner,
        evidence_by_candidate={
            "cand-h2": _EvidenceSpec(
                baseline_seconds=(180.0, 181.0, 179.0, 182.0, 178.0),
                candidate_seconds=(50.0, 51.0, 49.0, 52.0, 50.0),
            ),
        },
        config=DispatcherConfig(max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(repo)

    assert [v.candidate_id for v in verdicts] == ["cand-h1", "cand-h2"]
    assert verdicts[0].keep is False
    assert verdicts[0].reason == DISALLOWED_PATH_REASON
    assert verdicts[1].keep is True
    assert verdicts[1].sla_met is True
    assert dispatcher.evidence_base_shas == [("cand-h2", "sha-default")]
