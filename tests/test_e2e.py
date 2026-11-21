import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pit_crew import managed_agent, tools
from pit_crew.contracts import Candidate, Evidence, Hypothesis, ReplayPlan, Verdict
from pit_crew.dispatcher import DISALLOWED_PATH_REASON, Dispatcher, DispatcherConfig, LaneRunner
from pit_crew.github import WorkflowRunResult
from pit_crew.ledger import Ledger, SqliteLedger
from pit_crew.server import create_app

REPO = "acme/checkout-service"
BASE_SHA = "1" * 40
RED_COMMIT_SHA = "deadbeef" + "2" * 32

BASE_WORKFLOW = """\
name: CI
on: [push, pull_request, workflow_dispatch]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest
"""

OPT_WORKFLOW_1 = BASE_WORKFLOW.replace(
    'python-version: "3.12"',
    'python-version: "3.12"\n          cache: pip',
)

OPT_WORKFLOW_2 = OPT_WORKFLOW_1.replace("run: pytest", "run: pytest -n auto")


@dataclass
class _FakeStep:
    type: str
    id: str
    name: str
    arguments: dict[str, Any] | None = None


@dataclass
class _FakeInteraction:
    id: str
    status: str | None
    steps: list[_FakeStep] | None = None
    continuation_token: str | None = None
    environment_id: str | None = "env-e2e-1"


class _ScriptedInteractionsResource:
    """Deterministic ``InteractionsResourceLike`` driving ``managed_agent.run_lane`` + ``tools.call``."""

    def __init__(self, step_builder: Callable[[str, str], list[_FakeStep]]) -> None:
        self._step_builder = step_builder
        self._seq = 0
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _FakeInteraction:
        self.requests.append(kwargs)
        self._seq += 1
        if "previous_interaction_id" in kwargs:
            return _FakeInteraction(id=f"int-{self._seq}", status="completed")
        raw_input = str(kwargs.get("input", ""))
        header, _, prompt = raw_input.partition("\n\n")
        lane = header.removeprefix("Lane: ").strip()
        steps = self._step_builder(lane, prompt)
        if not steps:
            return _FakeInteraction(id=f"int-{self._seq}", status="completed")
        return _FakeInteraction(id=f"int-{self._seq}", status="requires_action", steps=steps)

    def get(self, interaction_id: str, /) -> _FakeInteraction:
        raise AssertionError(interaction_id)


class _ScriptedAgentClient:
    def __init__(self, step_builder: Callable[[str, str], list[_FakeStep]]) -> None:
        self.interactions = _ScriptedInteractionsResource(step_builder)


class _FakeE2EGitHubClient:
    """Thread-safe offline ``GitHubClient`` for M1 end-to-end integration tests."""

    def __init__(
        self,
        default_sha: str = BASE_SHA,
        *,
        run_resolver: Callable[[str, int], tuple[str, float]] | None = None,
        files: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._default_sha = default_sha
        self._run_resolver = run_resolver
        self._files = dict(files or {})
        self.branches: dict[str, str] = {}
        self.commits: list[dict[str, Any]] = []
        self.dispatches: list[tuple[str, str, str]] = []
        self.pull_requests: list[dict[str, str]] = []
        self._runs: dict[int, WorkflowRunResult] = {}

    def get_default_branch_sha(self, _repo: str) -> str:
        return self._default_sha

    def create_branch(self, _repo: str, branch: str, base_sha: str) -> str:
        with self._lock:
            self.branches[branch] = base_sha
            return base_sha

    def delete_branch(self, _repo: str, branch: str) -> None:
        with self._lock:
            self.branches.pop(branch, None)

    def commit_files(
        self,
        repo: str,
        branch: str,
        base_sha: str,
        files: dict[str, str],
        message: str,
    ) -> str:
        with self._lock:
            head_sha = f"head-sha-{len(self.commits) + 1:04d}" + "a" * 27
            self.branches[branch] = head_sha
            for path, content in files.items():
                self._files[(head_sha, path)] = content
            self.commits.append(
                {"repo": repo, "branch": branch, "base_sha": base_sha, "files": dict(files), "message": message}
            )
            return head_sha

    def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> int:
        with self._lock:
            assert ref in self.branches, f"Dispatched unknown branch ref {ref!r}"
            self.dispatches.append((repo, workflow, ref))
            run_id = len(self.dispatches)
            conclusion, duration = self._run_resolver(ref, run_id) if self._run_resolver else ("success", 45.0)
            self._runs[run_id] = WorkflowRunResult(
                run_id=run_id,
                status="completed",
                conclusion=conclusion,
                duration_seconds=duration,
                html_url=f"https://github.com/{repo}/actions/runs/{run_id}",
                head_sha=self.branches[ref],
                head_branch=ref,
            )
            return run_id

    def get_workflow_run(self, _repo: str, run_id: int) -> WorkflowRunResult:
        with self._lock:
            return self._runs[run_id]

    def get_file_content(self, _repo: str, path: str, ref: str) -> str | None:
        with self._lock:
            return self._files.get((ref, path))

    def open_pull_request(self, repo: str, *, title: str, body: str, head: str, base: str) -> str:
        with self._lock:
            pr_num = len(self.pull_requests) + 1
            url = f"https://github.com/{repo}/pull/{pr_num}"
            self.pull_requests.append(
                {"repo": repo, "title": title, "body": body, "head": head, "base": base, "url": url}
            )
            return url


def _context_from_prompt(prompt: str) -> dict[str, Any]:
    _, raw = prompt.split("Context:\n", maxsplit=1)
    parsed: dict[str, Any] = json.loads(raw)
    return parsed


def _make_lane_runner(client: _ScriptedAgentClient) -> LaneRunner:
    def runner(lane: str, task: str, ledger: Ledger | None = None, *, repo: str | None = None) -> object:
        return managed_agent.run_lane(lane, task, ledger, repo=repo, client=client)

    return runner


def test_m1_scenario_1_proven_keep_meets_sla_opens_pr_and_serves_pit_wall_api(tmp_path: Path) -> None:
    """Scenario 1: Full Hypothesis -> Candidate -> ReplayPlan -> Evidence -> Verdict(KEEP) -> PR -> Pit Wall API."""
    ledger = SqliteLedger(tmp_path / "m1_keep.db")

    def steps(lane: str, prompt: str) -> list[_FakeStep]:
        ctx = _context_from_prompt(prompt)
        if lane == "analysis":
            return [
                _FakeStep(
                    type="function_call",
                    id="call-a1",
                    name="record_hypothesis",
                    arguments={
                        "repo": REPO,
                        "description": "Pip downloads wheels from scratch on every CI run",
                        "skill": "dep-cache",
                        "expected_savings_seconds": 55.0,
                        "workflow_path": ".github/workflows/ci.yml",
                        "job": "test",
                        "step": "Install dependencies",
                        "bucket": "setup",
                        "base_sha": ctx["base_sha"],
                    },
                )
            ]
        if lane == "optimizer":
            hyp_id = ctx["hypothesis"]["id"]
            return [
                _FakeStep(
                    type="function_call",
                    id="call-o1",
                    name="report_candidate",
                    arguments={
                        "hypothesis_id": hyp_id,
                        "branch": f"pc/{hyp_id}-dep-cache",
                        "summary": "Cache pip wheels in setup-python",
                        "files": {".github/workflows/ci.yml": OPT_WORKFLOW_1},
                        "original_files": {".github/workflows/ci.yml": BASE_WORKFLOW},
                        "base_sha": ctx["base_sha"],
                    },
                )
            ]
        cand_id = ctx["candidate"]["id"]
        return [
            _FakeStep(
                type="function_call",
                id="call-p1",
                name="plan_replays",
                arguments={
                    "candidate_id": cand_id,
                    "commits": [
                        {
                            "sha": RED_COMMIT_SHA,
                            "expected_checks": ["CI/test"],
                            "reason": "historical rounding regression",
                        }
                    ],
                    "mutations": [
                        {
                            "path": "app/pricing.py",
                            "content": "def total(items):\n    return 0\n",
                            "reason": "zero-out cart total",
                            "expected_checks": ["CI/test"],
                        }
                    ],
                },
            )
        ]

    def resolve_run(ref: str, _run_id: int) -> tuple[str, float]:
        if ref.startswith("pc/replay-"):
            return ("failure", 14.0)
        if ref.startswith("pc/base-"):
            return ("success", 110.0)
        return ("success", 46.0)

    github = _FakeE2EGitHubClient(
        files={
            (BASE_SHA, ".github/workflows/ci.yml"): BASE_WORKFLOW,
            (RED_COMMIT_SHA, ".github/workflows/ci.yml"): BASE_WORKFLOW,
        },
        run_resolver=resolve_run,
    )
    agent_client = _ScriptedAgentClient(steps)
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_make_lane_runner(agent_client),
        config=DispatcherConfig(timing_pairs=5, max_parallel_runs=4),
    )

    verdict = dispatcher.run_cycle(REPO)

    assert verdict is not None
    assert verdict.keep is True
    assert verdict.sla_met is True
    assert verdict.median_seconds == 46.0
    assert verdict.p95_seconds == 46.0
    assert verdict.speedup > 2.0
    assert verdict.p_value is not None
    assert verdict.p_value <= 0.05
    assert len(github.pull_requests) == 1
    assert github.pull_requests[0]["base"] == "main"

    published_cand = ledger.get(Candidate, verdict.candidate_id)
    assert published_cand.head_sha.startswith("head-sha-")

    with TestClient(create_app(ledger, dispatcher=dispatcher)) as api:
        status = api.get(f"/api/repos/{REPO}/status").json()
        attempts = api.get(f"/api/repos/{REPO}/attempts").json()
        records = api.get(f"/api/repos/{REPO}/records").json()

    assert status["repo"] == REPO
    assert status["active"] is False
    assert status["sla_met"] is True
    assert status["base_sha"] == published_cand.head_sha
    assert status["latest_verdict"]["id"] == verdict.id
    assert len(attempts) == 1
    assert attempts[0]["keep"] is True
    assert attempts[0]["rated"] is True
    assert [len(records[k]) for k in ("hypotheses", "candidates", "replay_plans", "evidence", "verdicts")] == [
        1,
        1,
        1,
        1,
        1,
    ]
    assert records["evidence"][0]["bugs_caught"] == 2
    assert records["evidence"][0]["bugs_total"] == 2


def test_m1_scenario_2_fast_but_unsafe_candidate_is_rejected_without_pr(tmp_path: Path) -> None:
    """Scenario 2: Candidate is ~1.9x faster (p < 0.05) but misses 1 of 4 bugs -> REJECT, no PR."""
    ledger = SqliteLedger(tmp_path / "m1_unsafe.db")
    red_shas = [f"bad{i}" + "0" * 36 for i in range(4)]

    def steps(lane: str, prompt: str) -> list[_FakeStep]:
        ctx = _context_from_prompt(prompt)
        if lane == "analysis":
            return [
                _FakeStep(
                    type="function_call",
                    id="c-a",
                    name="record_hypothesis",
                    arguments={
                        "repo": REPO,
                        "description": "Slow full pytest suite",
                        "skill": "dep-cache",
                        "expected_savings_seconds": 60.0,
                        "workflow_path": ".github/workflows/ci.yml",
                    },
                )
            ]
        if lane == "optimizer":
            hyp_id = ctx["hypothesis"]["id"]
            return [
                _FakeStep(
                    type="function_call",
                    id="c-o",
                    name="report_candidate",
                    arguments={
                        "hypothesis_id": hyp_id,
                        "branch": f"pc/{hyp_id}-fast-unsafe",
                        "summary": "Aggressive cache configuration",
                        "files": {".github/workflows/ci.yml": OPT_WORKFLOW_1},
                        "original_files": {".github/workflows/ci.yml": BASE_WORKFLOW},
                    },
                )
            ]
        return [
            _FakeStep(
                type="function_call",
                id="c-p",
                name="plan_replays",
                arguments={"candidate_id": ctx["candidate"]["id"], "bug_shas": red_shas},
            )
        ]

    def resolve_run(ref: str, _run_id: int) -> tuple[str, float]:
        if ref.startswith("pc/replay-"):
            # Misses bug index 2 (passes instead of failing)!
            return ("success", 12.0) if ref.endswith("-2") else ("failure", 12.0)
        if ref.startswith("pc/baseline-"):
            return ("success", 120.0)
        return ("success", 62.0)

    files = {(BASE_SHA, ".github/workflows/ci.yml"): BASE_WORKFLOW}
    for sha in red_shas:
        files[(sha, ".github/workflows/ci.yml")] = BASE_WORKFLOW

    github = _FakeE2EGitHubClient(files=files, run_resolver=resolve_run)
    dispatcher = Dispatcher(ledger, github, lane_runner=_make_lane_runner(_ScriptedAgentClient(steps)))

    verdict = dispatcher.run_cycle(REPO)

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason == "caught 3/4 bugs"
    assert github.pull_requests == []
    prior = ledger.prior_attempts(REPO)
    assert len(prior) == 1
    assert prior[0]["keep"] is False
    assert prior[0]["reason"] == "caught 3/4 bugs"


def test_m1_scenario_3_disallowed_path_rejected_with_zero_ci_dispatches(tmp_path: Path) -> None:
    """Scenario 3: Candidate touching src/app.py is blocked both by tool guard and Dispatcher with 0 CI runs."""
    ledger = SqliteLedger(tmp_path / "m1_disallowed.db")
    hyp_id = str(
        tools.call(
            "record_hypothesis",
            ledger,
            {
                "repo": REPO,
                "description": "Shortcut application code",
                "skill": "affected-tests",
                "expected_savings_seconds": 50.0,
                "workflow_path": ".github/workflows/ci.yml",
            },
        )
    )

    # Layer 1: managed_agent.run_lane + report_candidate rejects src/app.py inside tool execution.
    bad_client = _ScriptedAgentClient(
        lambda _lane, _prompt: [
            _FakeStep(
                type="function_call",
                id="bad-1",
                name="report_candidate",
                arguments={
                    "hypothesis_id": hyp_id,
                    "branch": f"pc/{hyp_id}-bad",
                    "summary": "edit app source",
                    "files": {"src/app.py": "FAST = True\n"},
                    "original_files": {"src/app.py": "FAST = False\n"},
                },
            )
        ]
    )
    managed_agent.run_lane("optimizer", "try bad path", ledger, repo=REPO, client=bad_client)
    assert ledger.all(Candidate) == []
    tool_reply = bad_client.interactions.requests[1]["input"][0]  # type: ignore[index]
    assert tool_reply["is_error"] is True
    assert "src/app.py" in tool_reply["result"]

    # Layer 2: Even if an unvalidated Candidate modifying src/app.py exists in SqliteLedger,
    # Dispatcher immediately rejects it before any branch creation, Proof run, or CI dispatch.
    rogue = Candidate(
        hypothesis_id=hyp_id,
        branch=f"pc/{hyp_id}-rogue",
        summary="modify src/app.py",
        repo=REPO,
        base_sha=BASE_SHA,
        files={"src/app.py": "FAST = True\n"},
    )
    ledger.add(rogue)

    github = _FakeE2EGitHubClient()
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("no lane should run")),
    )
    verdict = dispatcher.run_cycle(REPO)

    assert verdict is not None
    assert verdict.keep is False
    assert verdict.reason == DISALLOWED_PATH_REASON
    assert github.dispatches == []
    assert github.commits == []
    assert github.pull_requests == []


def test_m1_scenario_4_compounding_run_until_sla_advances_base_sha(tmp_path: Path) -> None:
    """Scenario 4: Cycle 1 keeps (85s, SLA not met) -> advances base_sha -> Cycle 2 keeps (48s, SLA met) -> stops."""
    ledger = SqliteLedger(tmp_path / "m1_compound.db")

    def steps(lane: str, prompt: str) -> list[_FakeStep]:
        ctx = _context_from_prompt(prompt)
        if lane == "analysis":
            if ledger.hypotheses_for_repo(REPO):
                return []
            return [
                _FakeStep(
                    type="function_call",
                    id="h-1",
                    name="record_hypothesis",
                    arguments={
                        "repo": REPO,
                        "description": "Cache pip wheels",
                        "skill": "dep-cache",
                        "expected_savings_seconds": 55.0,
                        "workflow_path": ".github/workflows/ci.yml",
                        "job": "test",
                        "step": "Install dependencies",
                        "base_sha": ctx["base_sha"],
                    },
                ),
                _FakeStep(
                    type="function_call",
                    id="h-2",
                    name="record_hypothesis",
                    arguments={
                        "repo": REPO,
                        "description": "Shard pytest across workers",
                        "skill": "shard-by-timing",
                        "expected_savings_seconds": 40.0,
                        "workflow_path": ".github/workflows/ci.yml",
                        "job": "test",
                        "step": "Run tests",
                        "base_sha": ctx["base_sha"],
                    },
                ),
            ]
        if lane == "optimizer":
            hyp = ctx["hypothesis"]
            skill = hyp["skill"]
            before = BASE_WORKFLOW if skill == "dep-cache" else OPT_WORKFLOW_1
            after = OPT_WORKFLOW_1 if skill == "dep-cache" else OPT_WORKFLOW_2
            return [
                _FakeStep(
                    type="function_call",
                    id="o-1",
                    name="report_candidate",
                    arguments={
                        "hypothesis_id": hyp["id"],
                        "branch": f"pc/{hyp['id']}-{skill}",
                        "summary": f"Apply {skill}",
                        "files": {".github/workflows/ci.yml": after},
                        "original_files": {".github/workflows/ci.yml": before},
                        "base_sha": ctx["base_sha"],
                    },
                )
            ]
        return [
            _FakeStep(
                type="function_call",
                id="p-1",
                name="plan_replays",
                arguments={"candidate_id": ctx["candidate"]["id"], "bug_shas": [RED_COMMIT_SHA]},
            )
        ]

    cycle_counter = {"cand_Idx": 0}

    def resolve_run(ref: str, _run_id: int) -> tuple[str, float]:
        if ref.startswith("pc/replay-"):
            return ("failure", 12.0)
        # Cycle 1 candidate branch ends with "-dep-cache"; Cycle 2 ends with "-shard-by-timing".
        if ref.endswith("-dep-cache"):
            return ("success", 85.0)
        if ref.endswith("-shard-by-timing"):
            cycle_counter["cand_Idx"] += 1
            return ("success", 82.0 if cycle_counter["cand_Idx"] == 1 else 48.0)
        # Baseline is 140s on Cycle 1 (BASE_SHA) and 85s on Cycle 2 (Cycle 1's head_sha).
        sha = github.branches[ref]
        return ("success", 140.0 if sha == BASE_SHA else 85.0)

    github = _FakeE2EGitHubClient(
        files={
            (BASE_SHA, ".github/workflows/ci.yml"): BASE_WORKFLOW,
            (RED_COMMIT_SHA, ".github/workflows/ci.yml"): BASE_WORKFLOW,
        },
        run_resolver=resolve_run,
    )
    dispatcher = Dispatcher(
        ledger,
        github,
        lane_runner=_make_lane_runner(_ScriptedAgentClient(steps)),
        config=DispatcherConfig(timing_pairs=5, max_compounding_cycles=5),
    )

    verdicts = dispatcher.run_until_sla(REPO)

    assert len(verdicts) == 2
    assert (verdicts[0].keep, verdicts[0].sla_met, verdicts[0].median_seconds) == (True, False, 85.0)
    assert (verdicts[1].keep, verdicts[1].sla_met, verdicts[1].median_seconds) == (True, True, 48.0)
    assert verdicts[1].p95_seconds < 120.0

    c1 = ledger.get(Candidate, verdicts[0].candidate_id)
    c2 = ledger.get(Candidate, verdicts[1].candidate_id)
    assert c1.base_sha == BASE_SHA
    assert c2.base_sha == c1.head_sha
    assert len(github.pull_requests) == 2

    with TestClient(create_app(ledger, dispatcher=dispatcher)) as api:
        status = api.get(f"/api/repos/{REPO}/status").json()
        attempts = api.get(f"/api/repos/{REPO}/attempts").json()
    assert status["base_sha"] == c2.head_sha
    assert status["sla_met"] is True
    assert len(attempts) == 2
    assert len(ledger.all(Evidence)) == 2
    assert len(ledger.all(ReplayPlan)) == 2
    assert len(ledger.all(Hypothesis)) == 2
    assert len(ledger.all(Verdict)) == 2
