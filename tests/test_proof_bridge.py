import json
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial

import pytest

from pit_crew import managed_agent, rater
from pit_crew.contracts import Candidate, Evidence, Verdict, is_allowed_candidate_path
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.miner import JobRun
from pit_crew.lanes.proof.verify import Replay
from pit_crew.ledger import InMemoryLedger, Ledger
from pit_crew.proof_bridge import (
    PROOF_CARD_PATH,
    ProofBridge,
    ProofBridgeError,
    ProofPRConfig,
    ReplayRunPair,
)

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
        run_outputs: dict[tuple[str, str], bytes | Exception] | None = None,
    ) -> None:
        self._files = dict(files or {})
        self._job_runs = list(job_runs)
        self._run_outputs = dict(run_outputs or {})
        self.file_requests: list[tuple[str, str, str]] = []
        self.job_run_requests: list[str] = []
        self.failure_output_requests: list[tuple[str, str]] = []

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        self.file_requests.append((repo, ref, path))
        return self._files.get((repo, ref, path))

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        self.job_run_requests.append(repo)
        return list(self._job_runs)

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        self.failure_output_requests.append((repo, run_id))
        result = self._run_outputs[(repo, run_id)]
        if isinstance(result, Exception):
            raise result
        return result


@dataclass(frozen=True)
class _FakeResponse:
    text: str | None


class _FakeModels:
    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> _FakeResponse:
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        return _FakeResponse(text=self._reply)


class _FakeModelsClient:
    def __init__(self, reply: str | Exception) -> None:
        self.models = _FakeModels(reply)


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

    with pytest.raises(ProofBridgeError, match="ask callable"):
        bridge.verify_replay_runs("acme/demo", ())


def test_verify_replay_runs_accepts_matching_failure_and_rater_keeps() -> None:
    repo = "acme/demo"
    github = FakeProofGitHub(
        run_outputs={
            (repo, "run-base-1"): b"baseline-mp4-bytes",
            (repo, "run-cand-1"): b"candidate-mp4-bytes",
        }
    )
    fake_client = _FakeModelsClient(
        json.dumps(
            {
                "same": True,
                "baseline_failure": "AssertionError in test_auth.py::test_token_expiry",
                "candidate_failure": "AssertionError in test_auth.py::test_token_expiry",
                "reason": "Both runs fail on the same assertion in test_token_expiry.",
            }
        )
    )
    ask = partial(managed_agent.ask_multimodal, client=fake_client)
    bridge = ProofBridge(github, ask=ask)

    verification = bridge.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="run-base-1",
                candidate_run_id="run-cand-1",
            )
        ],
    )

    assert verification.bugs_total == 1
    assert verification.bugs_caught == 1
    assert verification.replays[0].status == "caught"
    assert verification.bug_outcomes[0]["status"] == "caught"
    assert verification.bug_outcomes[0]["same_failure"] is True
    assert github.failure_output_requests == [(repo, "run-base-1"), (repo, "run-cand-1")]
    assert len(fake_client.models.calls) == 1
    assert fake_client.models.calls[0]["model"] == "gemini-4-carbon"

    evidence = verification.to_evidence(
        candidate_id="cand-1",
        baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
        candidate_seconds=(50.0, 51.0, 49.0, 52.0, 48.0),
    )
    verdict = rater.rate(evidence)
    assert verdict.keep is True


def test_verify_replay_runs_rejects_different_failure_fail_closed() -> None:
    repo = "acme/demo"
    github = FakeProofGitHub(
        run_outputs={
            (repo, "run-base-1"): b"baseline-assertion-failure",
            (repo, "run-cand-1"): b"candidate-pip-install-crash",
        }
    )
    fake_client = _FakeModelsClient(
        json.dumps(
            {
                "same": False,
                "baseline_failure": "AssertionError in test_auth.py::test_token_expiry",
                "candidate_failure": "ModuleNotFoundError: No module named 'pytest'",
                "reason": "Candidate crashed during test setup instead of catching the bug.",
            }
        )
    )
    ask = partial(managed_agent.ask_multimodal, client=fake_client)
    bridge = ProofBridge(github, ask=ask)

    verification = bridge.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="run-base-1",
                candidate_run_id="run-cand-1",
            )
        ],
    )

    assert verification.bugs_total == 1
    assert verification.bugs_caught == 0
    assert verification.replays[0].status == "different failure"
    assert verification.bug_outcomes[0]["same_failure"] is False

    evidence = verification.to_evidence(
        candidate_id="cand-2",
        baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
        candidate_seconds=(50.0, 51.0, 49.0, 52.0, 48.0),
    )
    verdict = rater.rate(evidence)
    assert verdict.keep is False
    assert verdict.reason == "caught 0/1 bugs"


@pytest.mark.parametrize(
    ("run_outputs", "reply"),
    [
        (
            {("acme/demo", "run-base-1"): b"base", ("acme/demo", "run-cand-1"): b"cand"},
            "not valid json",
        ),
        (
            {("acme/demo", "run-base-1"): b"base", ("acme/demo", "run-cand-1"): b"cand"},
            RuntimeError("gemini unavailable"),
        ),
        (
            {("acme/demo", "run-base-1"): OSError("log download 404"), ("acme/demo", "run-cand-1"): b"cand"},
            json.dumps({"same": True, "baseline_failure": "a", "candidate_failure": "a", "reason": "same"}),
        ),
    ],
)
def test_verify_replay_runs_fails_closed_when_unverified(
    run_outputs: dict[tuple[str, str], bytes | Exception],
    reply: str | Exception,
) -> None:
    repo = "acme/demo"
    github = FakeProofGitHub(run_outputs=run_outputs)
    fake_client = _FakeModelsClient(reply)
    ask = partial(managed_agent.ask_multimodal, client=fake_client)
    bridge = ProofBridge(github)

    verification = bridge.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="run-base-1",
                candidate_run_id="run-cand-1",
            )
        ],
        ask=ask,
    )

    assert verification.bugs_total == 1
    assert verification.bugs_caught == 0
    assert verification.replays[0].status == "unverified"
    assert verification.bug_outcomes[0]["same_failure"] is None

    evidence = verification.to_evidence(
        candidate_id="cand-3",
        baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
        candidate_seconds=(50.0, 51.0, 49.0, 52.0, 48.0),
    )
    verdict = rater.rate(evidence)
    assert verdict.keep is False
    assert verdict.reason == "caught 0/1 bugs"


def test_verify_replay_runs_skips_verifier_when_not_both_failed() -> None:
    repo = "acme/demo"
    github = FakeProofGitHub()
    fake_client = _FakeModelsClient(AssertionError("verifier should not be called"))
    ask = partial(managed_agent.ask_multimodal, client=fake_client)
    bridge = ProofBridge(github, ask=ask)

    verification = bridge.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="1111aaaa",
                check="CI/Unit Tests",
                baseline_conclusion="success",
                candidate_conclusion="failure",
                baseline_run_id="run-base-1",
                candidate_run_id="run-cand-1",
            ),
            ReplayRunPair(
                sha="2222bbbb",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="success",
                baseline_run_id="run-base-2",
                candidate_run_id="run-cand-2",
            ),
        ],
    )

    assert [r.status for r in verification.replays] == ["not a bug", "missed"]
    assert verification.bugs_total == 1
    assert verification.bugs_caught == 0
    assert github.failure_output_requests == []
    assert fake_client.models.calls == []


class FakePRGitHub(FakeProofGitHub):
    def __init__(self) -> None:
        super().__init__()
        self.committed: list[tuple[str, str, dict[str, str], str]] = []
        self.opened_prs: list[dict[str, str]] = []

    def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        self.committed.append((repo, branch, dict(files), message))
        return "cardsha1234"

    def open_pull_request(self, repo: str, *, head: str, base: str, title: str, body: str) -> str:
        self.opened_prs.append({"repo": repo, "head": head, "base": base, "title": title, "body": body})
        return f"https://github.com/{repo}/pull/7"


def _kept_fixture() -> tuple[Candidate, Evidence, Verdict, tuple[Replay, ...]]:
    repo = "acme/demo"
    github = FakeProofGitHub(
        run_outputs={
            (repo, "run-base-1"): b"baseline-mp4-bytes",
            (repo, "run-cand-1"): b"candidate-mp4-bytes",
        }
    )
    fake_client = _FakeModelsClient(
        json.dumps(
            {
                "same": True,
                "baseline_failure": "AssertionError in test_auth.py::test_token_expiry",
                "candidate_failure": "AssertionError in test_auth.py::test_token_expiry",
                "reason": "Both runs fail on the same assertion in test_token_expiry.",
            }
        )
    )
    bridge = ProofBridge(github, ask=partial(managed_agent.ask_multimodal, client=fake_client))
    verification = bridge.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="run-base-1",
                candidate_run_id="run-cand-1",
            )
        ],
    )
    candidate = Candidate(
        id="cand-99",
        hypothesis_id="hyp-99",
        branch="pit-crew/cand-99",
        summary="Parallelize unit tests",
        repo=repo,
    )
    evidence = verification.to_evidence(
        candidate_id=candidate.id,
        baseline_seconds=(120.0, 121.0, 119.0, 122.0, 118.0),
        candidate_seconds=(50.0, 51.0, 49.0, 52.0, 48.0),
    )
    verdict = rater.rate(evidence)
    return candidate, evidence, verdict, verification.replays


def test_open_kept_pr_commits_svg_card_and_formats_pr_body() -> None:
    assert is_allowed_candidate_path(PROOF_CARD_PATH) is True

    candidate, evidence, verdict, replays = _kept_fixture()
    assert verdict.keep is True

    github = FakePRGitHub()
    bridge = ProofBridge(github)
    opened = bridge.open_kept_pr(
        candidate,
        evidence,
        verdict,
        replays,
        config=ProofPRConfig(auto_open_pr=True, base_branch="main"),
    )

    assert opened is not None
    assert opened.url == "https://github.com/acme/demo/pull/7"
    assert opened.card_path == ".github/pit-crew/proof-card.svg"
    assert opened.card_sha == "cardsha1234"
    expected_card_url = "https://raw.githubusercontent.com/acme/demo/cardsha1234/.github/pit-crew/proof-card.svg"
    assert opened.card_url == expected_card_url

    expected_svg = render_card(candidate.branch, evidence, verdict, replays)
    assert github.committed == [
        (
            "acme/demo",
            "pit-crew/cand-99",
            {".github/pit-crew/proof-card.svg": expected_svg},
            "pit-crew: add proof card for cand-99",
        )
    ]

    expected_body = pr_body(candidate.branch, evidence, verdict, replays, card_url=expected_card_url)
    assert opened.body == expected_body
    assert github.opened_prs == [
        {
            "repo": "acme/demo",
            "head": "pit-crew/cand-99",
            "base": "main",
            "title": "pit-crew: Parallelize unit tests",
            "body": expected_body,
        }
    ]


def test_open_kept_pr_skips_when_auto_open_pr_disabled() -> None:
    candidate, evidence, verdict, replays = _kept_fixture()
    assert verdict.keep is True

    github = FakePRGitHub()
    bridge = ProofBridge(github)

    assert bridge.open_kept_pr(candidate, evidence, verdict, replays) is None
    assert bridge.open_kept_pr(candidate, evidence, verdict, replays, config=ProofPRConfig(auto_open_pr=False)) is None
    assert github.committed == []
    assert github.opened_prs == []


def test_open_kept_pr_skips_when_verdict_rejected() -> None:
    candidate, evidence, _, replays = _kept_fixture()
    rejected = Verdict(
        candidate_id=candidate.id,
        keep=False,
        speedup=1.0,
        p_value=None,
        reason="caught 0/1 bugs",
    )

    github = FakePRGitHub()
    bridge = ProofBridge(github)
    result = bridge.open_kept_pr(
        candidate,
        evidence,
        rejected,
        replays,
        config=ProofPRConfig(auto_open_pr=True),
    )

    assert result is None
    assert github.committed == []
    assert github.opened_prs == []


def test_open_kept_pr_errors_on_missing_repo_or_pr_client() -> None:
    candidate, evidence, verdict, replays = _kept_fixture()
    no_repo_candidate = Candidate(
        id="cand-no-repo",
        hypothesis_id="hyp-1",
        branch="pit-crew/no-repo",
        summary="",
    )

    with pytest.raises(ProofBridgeError, match="no repo"):
        ProofBridge(FakePRGitHub()).open_kept_pr(
            no_repo_candidate,
            evidence,
            verdict,
            replays,
            config=ProofPRConfig(auto_open_pr=True),
        )

    with pytest.raises(ProofBridgeError, match="PullRequestClient"):
        ProofBridge(FakeProofGitHub()).open_kept_pr(
            candidate,
            evidence,
            verdict,
            replays,
            config=ProofPRConfig(auto_open_pr=True),
        )
