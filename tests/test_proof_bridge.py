from collections.abc import Sequence

import pytest

from pit_crew.contracts import Candidate
from pit_crew.lanes.proof.miner import JobRun
from pit_crew.ledger import InMemoryLedger, Ledger
from pit_crew.proof_bridge import ProofBridge, ProofBridgeError

BASE_CI = """\
name: CI
on: [push, pull_request]
jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - run: ruff check
  test:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""

CANDIDATE_CI_TOUCH_TEST_ONLY = """\
name: CI
on: [push, pull_request]
jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - run: ruff check
  test:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - run: pytest -n auto
"""

CANDIDATE_CI_TOUCH_BOTH = """\
name: CI
on: [push, pull_request]
jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - run: ruff check --cache-dir .ruff
  test:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - run: pytest -n auto
"""


class FakeProofGitHub:
    def __init__(
        self,
        files: dict[tuple[str, str, str], str] | None = None,
        job_runs: Sequence[JobRun] = (),
    ) -> None:
        self._files = dict(files or {})
        self._job_runs = list(job_runs)
        self.file_requests: list[tuple[str, str, str]] = []
        self.job_run_requests: list[str] = []

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        self.file_requests.append((repo, ref, path))
        return self._files.get((repo, ref, path))

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        self.job_run_requests.append(repo)
        return list(self._job_runs)


def _job_run(
    sha: str,
    job: str,
    conclusion: str,
    *,
    created_at: str = "2026-11-01T00:00:00Z",
    attempt: int = 1,
) -> JobRun:
    return JobRun(
        sha=sha,
        branch="main",
        created_at=created_at,
        workflow="CI",
        job=job,
        attempt=attempt,
        conclusion=conclusion,
    )


def test_prepare_context_computes_touched_red_replays_and_uncovered() -> None:
    repo = "acme/demo"
    base_sha = "aaaa1111"
    wf_path = ".github/workflows/ci.yml"
    github = FakeProofGitHub(
        files={(repo, base_sha, wf_path): BASE_CI},
        job_runs=[
            # Deterministic red commit on Unit Tests (later fixed)
            _job_run("1111aaaa", "Unit Tests (1/2)", "failure", created_at="2026-11-01T01:00:00Z"),
            _job_run("2222bbbb", "Unit Tests (1/2)", "success", created_at="2026-11-01T02:00:00Z"),
            # Flaky commit on Lint (failed attempt 1, passed attempt 2) -> excluded
            _job_run("3333cccc", "Lint", "failure", created_at="2026-11-01T03:00:00Z", attempt=1),
            _job_run("3333cccc", "Lint", "success", created_at="2026-11-01T03:05:00Z", attempt=2),
        ],
    )
    candidate = Candidate(
        hypothesis_id="hyp-1",
        branch="pit-crew/cand-1",
        summary="Cache lint and parallelize tests",
        repo=repo,
        base_sha=base_sha,
        head_sha="deadbeef",
        files={wf_path: CANDIDATE_CI_TOUCH_BOTH},
    )

    bridge = ProofBridge(github)
    ctx = bridge.prepare_context(candidate)

    assert ctx.repo == repo
    assert ctx.base_sha == base_sha
    assert ctx.touched_checks == frozenset(
        {
            frozenset({"CI/lint", "CI/Lint"}),
            frozenset({"CI/test", "CI/Unit Tests"}),
        }
    )
    assert [c.sha for c in ctx.red_commits] == ["1111aaaa"]
    assert ctx.red_commits[0].fixed is True
    assert ctx.flaky_checks == (("3333cccc", "CI/Lint"),)
    assert ctx.required_replays == ("1111aaaa",)
    assert ctx.uncovered_checks == frozenset({frozenset({"CI/lint", "CI/Lint"})})
    assert github.file_requests == [(repo, base_sha, wf_path)]
    assert github.job_run_requests == [repo]

    prompt = bridge.build_prompt(ctx)
    assert "## Required replays\n- 1111aaaa\n" in prompt
    assert "## Uncovered checks\n- CI/Lint | CI/lint\n" in prompt


def test_prepare_context_non_workflow_change_touches_all_checks() -> None:
    repo = "acme/demo"
    base_sha = "aaaa1111"
    github = FakeProofGitHub(
        files={(repo, base_sha, "pyproject.toml"): "[tool.pytest.ini_options]\n"},
        job_runs=[
            _job_run("1111aaaa", "Unit Tests", "failure", created_at="2026-11-01T01:00:00Z"),
        ],
    )
    candidate = Candidate(
        hypothesis_id="hyp-2",
        branch="pit-crew/cand-2",
        summary="Update pytest config",
        repo=repo,
        base_sha=base_sha,
        files={"pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-n auto'\n"},
    )

    bridge = ProofBridge(github)
    ctx = bridge.prepare_context(candidate)

    assert ctx.touched_checks is None
    assert ctx.required_replays == ("1111aaaa",)
    assert ctx.uncovered_checks == frozenset()

    prompt = bridge.build_prompt(ctx)
    assert "ALL checks" in prompt
    assert "Inspect repository workflows" in prompt


def test_prepare_context_with_no_files_and_changed_files_is_conservative() -> None:
    github = FakeProofGitHub(job_runs=[])
    candidate = Candidate(
        hypothesis_id="hyp-3",
        branch="pit-crew/cand-3",
        summary="Pre-published candidate without inline files",
        repo="acme/demo",
        changed_files=(".github/workflows/ci.yml",),
    )

    ctx = ProofBridge(github).prepare_context(candidate)

    assert ctx.touched_checks is None
    assert ctx.required_replays == ()
    assert ctx.uncovered_checks == frozenset()


def test_prepare_context_with_empty_candidate_has_no_touched_checks() -> None:
    github = FakeProofGitHub(job_runs=[])
    candidate = Candidate(
        hypothesis_id="hyp-4",
        branch="pit-crew/cand-4",
        summary="No-op",
        repo="acme/demo",
    )

    ctx = ProofBridge(github).prepare_context(candidate)

    assert ctx.touched_checks == frozenset()
    prompt = ProofBridge(github).build_prompt(ctx)
    assert "## Touched checks\n- (none)" in prompt
    assert "## Required replays\n- (none)" in prompt
    assert "## Uncovered checks\n- (none)" in prompt
    assert "## Red commits (deterministic failures, newest first)\n- (none)" in prompt


def test_run_injects_context_into_proof_lane_prompt() -> None:
    repo = "acme/demo"
    base_sha = "aaaa1111"
    wf_path = ".github/workflows/ci.yml"
    github = FakeProofGitHub(
        files={(repo, base_sha, wf_path): BASE_CI},
        job_runs=[
            _job_run("1111aaaa", "Unit Tests", "failure", created_at="2026-11-01T01:00:00Z"),
            _job_run("2222bbbb", "Unit Tests", "success", created_at="2026-11-01T02:00:00Z"),
            _job_run("3333cccc", "Lint", "failure", created_at="2026-11-01T03:00:00Z", attempt=1),
            _job_run("3333cccc", "Lint", "success", created_at="2026-11-01T03:05:00Z", attempt=2),
        ],
    )
    candidate = Candidate(
        id="cand-42",
        hypothesis_id="hyp-42",
        branch="pit-crew/cand-42",
        summary="Parallelize pytest",
        repo=repo,
        base_sha=base_sha,
        head_sha="beefcafe",
        files={wf_path: CANDIDATE_CI_TOUCH_TEST_ONLY},
    )
    ledger = InMemoryLedger()
    captured: dict[str, object] = {}

    def fake_run_lane(lane: str, task: str, *, repo: str = "", ledger: Ledger | None = None) -> str:
        captured["lane"] = lane
        captured["task"] = task
        captured["repo"] = repo
        captured["ledger"] = ledger
        return "interaction-123"

    bridge = ProofBridge(github, run_lane=fake_run_lane)
    ctx, result = bridge.run(candidate, ledger=ledger, extra_instructions="Focus on xdist isolation.")

    assert result == "interaction-123"
    assert ctx.required_replays == ("1111aaaa",)
    assert ctx.uncovered_checks == frozenset()
    assert captured["lane"] == "proof"
    assert captured["repo"] == repo
    assert captured["ledger"] is ledger
    prompt = str(captured["task"])
    assert "cand-42" in prompt
    assert "CI/Unit Tests | CI/test" in prompt
    assert "## Required replays\n- 1111aaaa\n" in prompt
    assert "## Uncovered checks\n- (none)\n" in prompt
    assert "1111aaaa [fixed]" in prompt
    assert "3333cccc (CI/Lint)" in prompt
    assert "Focus on xdist isolation." in prompt
    assert "plan_replays(candidate_id='cand-42'" in prompt


def test_errors_on_missing_repo_or_runner() -> None:
    github = FakeProofGitHub()
    candidate = Candidate(hypothesis_id="hyp-1", branch="b", summary="s")
    bridge = ProofBridge(github)

    with pytest.raises(ProofBridgeError, match="no repo"):
        bridge.prepare_context(candidate)

    with pytest.raises(ProofBridgeError, match="run_lane"):
        bridge.run(candidate, repo="acme/demo")
