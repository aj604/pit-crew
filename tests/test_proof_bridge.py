import io
import json
import subprocess
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest

from pit_crew import managed_agent, rater
from pit_crew.contracts import Candidate, Evidence, Verdict, is_allowed_candidate_path
from pit_crew.lanes.proof.artifacts import MediaPart, score_artifact_name
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.miner import JobRun
from pit_crew.lanes.proof.verify import Replay
from pit_crew.ledger import InMemoryLedger, Ledger
from pit_crew.proof_bridge import (
    MEDIA_URL_PREFIX,
    PROOF_CARD_PATH,
    MultimodalReplayVerifier,
    ProofBridge,
    ProofBridgeError,
    ProofPRConfig,
    ReplayRunPair,
    default_media_dir,
    extract_mp4_bytes,
    is_safe_mp4_filename,
    persist_replay_mp4,
    verify_replay_run_pairs,
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
    def __init__(self, *, run_outputs: dict[tuple[str, str], bytes | Exception] | None = None) -> None:
        super().__init__(run_outputs=run_outputs)
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


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_multimodal_replay_verifier_extracts_mp4_from_artifact_zip_and_fails_closed_without_mp4() -> None:
    repo = "acme/demo"
    base_zip = _zip_bytes({"step-2.cast": b'{"version":2}\n', "step-2.mp4": b"base-mp4-video"})
    cand_zip = _zip_bytes({"step-2.cast": b'{"version":2}\n', "step-2.mp4": b"cand-mp4-video"})
    cast_only_zip = _zip_bytes({"step-2.cast": b'{"version":2}\n'})

    github = FakeProofGitHub(
        run_outputs={
            (repo, "101"): base_zip,
            (repo, "102"): cand_zip,
            (repo, "103"): cast_only_zip,
        }
    )
    fake_client = _FakeModelsClient(
        json.dumps(
            {
                "same": True,
                "baseline_failure": "AssertionError: expected 3, got 2",
                "candidate_failure": "AssertionError: expected 3, got 2",
                "reason": "Identical assertion failure.",
            }
        )
    )
    verifier = MultimodalReplayVerifier(github, ask=partial(managed_agent.ask_multimodal, client=fake_client))

    matched = verifier.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="101",
                candidate_run_id="102",
            )
        ],
    )
    assert matched.bugs_total == 1
    assert matched.bugs_caught == 1
    assert matched.replays[0].status == "caught"
    # Verify extracted MP4 bytes (not raw ZIP bytes) were passed to Gemini.
    contents = fake_client.models.calls[0]["contents"]
    assert isinstance(contents, list)
    media_bytes = [part.inline_data.data for part in contents if getattr(part, "inline_data", None) is not None]
    assert media_bytes == [b"base-mp4-video", b"cand-mp4-video"]

    # Missing MP4 inside ZIP (e.g. agg/ffmpeg unavailable) fails closed as unverified.
    unverified = verifier.verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Unit Tests",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="101",
                candidate_run_id="103",
            )
        ],
    )
    assert unverified.bugs_total == 1
    assert unverified.bugs_caught == 0
    assert unverified.replays[0].status == "unverified"
    assert "ReplayArtifactError" in str(unverified.bug_outcomes[0]["seen"])


def test_multimodal_replay_verifier_rejects_unrelated_job_failures_and_metadata_mismatch() -> None:
    repo = "acme/vite"
    node26_meta = json.dumps(
        {"workflow": "CI", "job_id": "test", "job_name": "Build&Test: node-26, ubuntu-latest"}
    ).encode()
    lint_meta = json.dumps({"workflow": "CI", "job_id": "lint", "job_name": "Lint: node-26, ubuntu-latest"}).encode()
    node26_zip = _zip_bytes({"replay-meta.json": node26_meta, "step-1.mp4": b"node26-video"})
    lint_zip = _zip_bytes({"replay-meta.json": lint_meta, "step-1.mp4": b"lint-video"})

    class _JobAwareGitHub(FakeProofGitHub):
        def __init__(
            self,
            failed_by_run: dict[str, list[str]],
            run_outputs: dict[tuple[str, str], bytes | Exception],
        ) -> None:
            super().__init__(run_outputs=run_outputs)
            self._failed_by_run = failed_by_run

        def list_failed_job_names(self, _repo: str, run_id: str) -> list[str]:
            return list(self._failed_by_run.get(run_id, []))

    fake_client = _FakeModelsClient(
        json.dumps({"same": True, "baseline_failure": "err", "candidate_failure": "err", "reason": "same"})
    )
    ask = partial(managed_agent.ask_multimodal, client=fake_client)

    # 1. Baseline failed on Node 26, but candidate passed Node 26 and failed on unrelated Lint -> different failure.
    gh_cand_wrong = _JobAwareGitHub(
        failed_by_run={
            "base-1": ["Build&Test: node-26, ubuntu-latest"],
            "cand-1": ["Lint: node-26, ubuntu-latest"],
        },
        run_outputs={(repo, "base-1"): node26_zip, (repo, "cand-1"): lint_zip},
    )
    res1 = MultimodalReplayVerifier(gh_cand_wrong, ask=ask).verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Build&Test: node-26, ubuntu-latest",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="base-1",
                candidate_run_id="cand-1",
                expected_checks=("CI/Build&Test: node-26, ubuntu-latest",),
            )
        ],
    )
    assert res1.bugs_total == 1
    assert res1.bugs_caught == 0
    assert res1.replays[0].status == "different failure"
    assert fake_client.models.calls == []

    # 2. Baseline itself only failed on unrelated Lint (Node 26 passed) -> unverified, model never called.
    gh_base_wrong = _JobAwareGitHub(
        failed_by_run={
            "base-2": ["Lint: node-26, ubuntu-latest"],
            "cand-2": ["Lint: node-26, ubuntu-latest"],
        },
        run_outputs={(repo, "base-2"): lint_zip, (repo, "cand-2"): lint_zip},
    )
    res2 = MultimodalReplayVerifier(gh_base_wrong, ask=ask).verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Build&Test: node-26, ubuntu-latest",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="base-2",
                candidate_run_id="cand-2",
                expected_checks=("CI/Build&Test: node-26, ubuntu-latest",),
            )
        ],
    )
    assert res2.bugs_total == 1
    assert res2.bugs_caught == 0
    assert res2.replays[0].status == "unverified"
    assert "did not fail on baseline" in str(res2.bug_outcomes[0]["seen"])
    assert fake_client.models.calls == []

    # 3. Both runs failed on Node 26, but candidate ZIP metadata proves it came from Lint -> unverified.
    gh_meta_mismatch = _JobAwareGitHub(
        failed_by_run={
            "base-3": ["Build&Test: node-26, ubuntu-latest"],
            "cand-3": ["Build&Test: node-26, ubuntu-latest", "Lint: node-26, ubuntu-latest"],
        },
        run_outputs={(repo, "base-3"): node26_zip, (repo, "cand-3"): lint_zip},
    )
    res3 = MultimodalReplayVerifier(gh_meta_mismatch, ask=ask).verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Build&Test: node-26, ubuntu-latest",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="base-3",
                candidate_run_id="cand-3",
                expected_checks=("CI/Build&Test: node-26, ubuntu-latest",),
            )
        ],
    )
    assert res3.bugs_total == 1
    assert res3.bugs_caught == 0
    assert res3.replays[0].status == "unverified"
    assert "ReplayArtifactError" in str(res3.bug_outcomes[0]["seen"])
    assert fake_client.models.calls == []

    # 4. ProofBridge.verify_replay_runs also fails closed when baseline failed on an unexpected check.
    bridge_res = ProofBridge(gh_base_wrong, ask=ask).verify_replay_runs(
        repo,
        [
            ReplayRunPair(
                sha="deadbeef",
                check="CI/Build&Test: node-26, ubuntu-latest",
                baseline_conclusion="failure",
                candidate_conclusion="failure",
                baseline_run_id="base-2",
                candidate_run_id="cand-2",
                expected_checks=("CI/Build&Test: node-26, ubuntu-latest",),
            )
        ],
    )
    assert bridge_res.bugs_caught == 0
    assert bridge_res.replays[0].status == "unverified"
    assert fake_client.models.calls == []


class _HttpxProofAdapter:
    def __init__(self, inner: object) -> None:
        from pit_crew.github import HttpxGitHubClient  # noqa: PLC0415

        assert isinstance(inner, HttpxGitHubClient)
        self._inner = inner

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        return self._inner.get_file_content(repo, path, ref)

    def list_workflow_job_runs(self, _repo: str) -> list[JobRun]:
        return []

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        return self._inner.get_run_failure_output(repo, run_id)

    def list_run_artifacts(self, repo: str, run_id: int | str) -> list[object]:
        return list(self._inner.list_run_artifacts(repo, run_id))

    def download_artifact(self, repo: str, artifact_id: int) -> bytes:
        return self._inner.download_artifact(repo, artifact_id)

    def get_failed_job_logs(  # noqa: PLR0913
        self,
        repo: str,
        run_id: int | str,
        *,
        check: str | None = None,
        expected_checks: Sequence[str] = (),
        check_aliases: Sequence[str] = (),
        max_bytes: int = 262_144,
    ) -> bytes:
        return self._inner.get_failed_job_logs(
            repo,
            run_id,
            check=check,
            expected_checks=expected_checks,
            check_aliases=check_aliases,
            max_bytes=max_bytes,
        )

    def list_failed_job_names(self, repo: str, run_id: int | str) -> list[str]:
        return self._inner.list_failed_job_names(repo, run_id)


def test_untargeted_sha_only_replay_and_job_id_aliases_verify_cleanly() -> None:
    import httpx  # noqa: PLC0415

    from pit_crew.github import HttpxGitHubClient  # noqa: PLC0415

    repo = "acme/repo"
    base_zip = _zip_bytes({"step-1.mp4": b"\x00\x00\x00\x18ftypmp42base"})
    cand_zip = _zip_bytes({"step-1.mp4": b"\x00\x00\x00\x18ftypmp42cand"})
    downloaded_log_jobs: list[str] = []

    routes: dict[str, httpx.Response] = {
        # Runs 101/102: untargeted SHA-only replay (pair.check='ci.yml', expected_checks=()).
        "/repos/acme/repo/actions/runs/101/artifacts": httpx.Response(
            200,
            json={"artifacts": [{"id": 1, "name": "pit-crew-replay-unit-test-0", "expired": False}]},
        ),
        "/repos/acme/repo/actions/runs/102/artifacts": httpx.Response(
            200,
            json={"artifacts": [{"id": 2, "name": "pit-crew-replay-unit-test-0", "expired": False}]},
        ),
        "/repos/acme/repo/actions/artifacts/1/zip": httpx.Response(200, content=base_zip),
        "/repos/acme/repo/actions/artifacts/2/zip": httpx.Response(200, content=cand_zip),
        # Runs 201/202: logs-only replay where expected_checks lists job ID before display name.
        "/repos/acme/repo/actions/runs/201/artifacts": httpx.Response(200, json={"artifacts": []}),
        "/repos/acme/repo/actions/runs/202/artifacts": httpx.Response(200, json={"artifacts": []}),
        "/repos/acme/repo/actions/runs/201/jobs": httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 21, "name": "Unit test", "conclusion": "failure", "workflow_name": "CI"},
                    {"id": 22, "name": "Lint", "conclusion": "failure", "workflow_name": "CI"},
                ]
            },
        ),
        "/repos/acme/repo/actions/runs/202/jobs": httpx.Response(
            200,
            json={
                "jobs": [
                    {"id": 31, "name": "Unit test", "conclusion": "failure", "workflow_name": "CI"},
                    {"id": 32, "name": "Lint", "conclusion": "failure", "workflow_name": "CI"},
                ]
            },
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/repos/acme/repo/actions/jobs/") and path.endswith("/logs"):
            downloaded_log_jobs.append(path.split("/")[-2])
            return httpx.Response(200, content=b"AssertionError: boom\n")
        return routes.get(path, httpx.Response(404))

    fake_client = _FakeModelsClient(
        json.dumps(
            {
                "same": True,
                "baseline_failure": "AssertionError: boom",
                "candidate_failure": "AssertionError: boom",
                "reason": "Matched.",
                "visual_evidence_seen": False,
                "visual_summary": "",
            }
        )
    )
    ask = partial(managed_agent.ask_multimodal, client=fake_client)

    with HttpxGitHubClient(token="ghp_test", transport=httpx.MockTransport(handler)) as gh:  # noqa: S106
        # 1. Untargeted SHA-only replay succeeds via verify_replay_run_pairs without filtering by 'ci.yml'.
        untargeted = verify_replay_run_pairs(
            gh,
            repo,
            [
                ReplayRunPair(
                    sha="deadbeef",
                    check="ci.yml",
                    baseline_conclusion="failure",
                    candidate_conclusion="failure",
                    baseline_run_id="101",
                    candidate_run_id="102",
                )
            ],
            ask=ask,
        )
        assert untargeted.bugs_total == 1
        assert untargeted.bugs_caught == 1
        assert untargeted.replays[0].status == "caught"

        # 2. ProofBridge resolves job-id-before-display-name aliases for logs-only runs and skips unrelated Lint logs.
        bridge = ProofBridge(_HttpxProofAdapter(gh), ask=ask)
        aliased = bridge.verify_replay_runs(
            repo,
            [
                ReplayRunPair(
                    sha="cafebabe",
                    check="CI/unit-test",
                    baseline_conclusion="failure",
                    candidate_conclusion="failure",
                    baseline_run_id="201",
                    candidate_run_id="202",
                    expected_checks=("CI/unit-test", "CI/Unit test"),
                )
            ],
        )
        assert aliased.bugs_total == 1
        assert aliased.bugs_caught == 1
        assert aliased.replays[0].status == "caught"
        assert downloaded_log_jobs == ["21", "31"]


def test_mp4_extraction_transcoding_and_atomic_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mp4_bytes = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp41-video-body"
    webm_bytes = b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01-webm-body"

    assert score_artifact_name("pit-crew-replay-test-0", "test")[0] < score_artifact_name("coverage-report", "test")[0]

    # Direct MP4 extraction wins over WebM.
    direct = extract_mp4_bytes(
        [
            MediaPart("browser.webm", "video/webm", webm_bytes, "browser_video"),
            MediaPart("terminal.mp4", "video/mp4", mp4_bytes, "terminal_video"),
        ]
    )
    assert direct == mp4_bytes

    # WebM transcode via ffmpeg returns valid MP4 bytes when ffmpeg succeeds.
    def fake_ffmpeg_ok(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(cmd[-1]).write_bytes(mp4_bytes)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_ok)
    converted = extract_mp4_bytes([MediaPart("browser.webm", "video/webm", webm_bytes, "browser_video")])
    assert converted == mp4_bytes

    # Missing ffmpeg degrades cleanly to None.
    def fake_ffmpeg_missing(_cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg_missing)
    assert extract_mp4_bytes([MediaPart("browser.webm", "video/webm", webm_bytes, "browser_video")]) is None
    assert extract_mp4_bytes([MediaPart("log.txt", "text/plain", b"hello", "log")]) is None

    # persist_replay_mp4 validates magic bytes, writes atomically, invokes hook, and survives hook failure.
    assert persist_replay_mp4(tmp_path, b"not-an-mp4", sha="abc", run_id="1", side="baseline") is None
    saved_hook_paths: list[Path] = []
    res = persist_replay_mp4(
        tmp_path,
        mp4_bytes,
        sha="../deadbeef",
        run_id="run/99",
        side="baseline",
        on_saved=saved_hook_paths.append,
    )
    assert res is not None
    media_url, file_path = res
    assert media_url.startswith(f"{MEDIA_URL_PREFIX}/")
    assert is_safe_mp4_filename(file_path.name)
    assert file_path.read_bytes() == mp4_bytes
    assert saved_hook_paths == [file_path]

    # Idempotent rewrite skips disk write and hook exception is non-fatal.
    def failing_hook(_path: Path) -> None:
        raise OSError

    res_again = persist_replay_mp4(
        tmp_path, mp4_bytes, sha="../deadbeef", run_id="run/99", side="baseline", on_saved=failing_hook
    )
    assert res_again == (media_url, file_path)

    # default_media_dir respects PIT_CREW_MEDIA_DIR, ledger sibling, and fallback.
    monkeypatch.delenv("PIT_CREW_MEDIA_DIR", raising=False)
    assert default_media_dir(tmp_path / "ledger.db") == (tmp_path / "media").resolve()
    assert default_media_dir(None) == Path(".pit_crew/media")
    monkeypatch.setenv("PIT_CREW_MEDIA_DIR", str(tmp_path / "custom-media"))
    assert default_media_dir(tmp_path / "ledger.db") == tmp_path / "custom-media"


def test_proof_bridge_and_verifier_persist_mp4_and_link_in_kept_pr(tmp_path: Path) -> None:
    repo = "acme/demo"
    base_mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00baseline-video"
    cand_mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00candidate-video"
    github = FakePRGitHub(
        run_outputs={
            (repo, "run-101"): base_mp4,
            (repo, "run-102"): cand_mp4,
        }
    )
    fake_client = _FakeModelsClient(
        json.dumps(
            {
                "same": True,
                "baseline_failure": "AssertionError: total mismatch",
                "candidate_failure": "AssertionError: total mismatch",
                "reason": "Identical failure recorded in both MP4 videos.",
                "visual_evidence_seen": True,
                "visual_summary": "Terminal shows AssertionError on both sides.",
            }
        )
    )
    ask = partial(managed_agent.ask_multimodal, client=fake_client)
    media_dir = tmp_path / "media"
    committed_hooks: list[Path] = []
    bridge = ProofBridge(
        github,
        ask=ask,
        media_dir=media_dir,
        on_media_saved=committed_hooks.append,
        public_base_url="https://pit-crew.example.test/",
    )
    assert bridge.media_dir == media_dir
    assert bridge.public_base_url == "https://pit-crew.example.test"

    pair = ReplayRunPair(
        sha="deadbeef1234",
        check="CI/Unit Tests",
        baseline_conclusion="failure",
        candidate_conclusion="failure",
        baseline_run_id="run-101",
        candidate_run_id="run-102",
        baseline_run_url="https://github.com/acme/demo/actions/runs/101",
        candidate_run_url="https://github.com/acme/demo/actions/runs/102",
    )
    verification = bridge.verify_replay_runs(repo, [pair])
    assert verification.bugs_total == 1
    assert verification.bugs_caught == 1
    assert len(verification.video_paths) == 2
    assert len(committed_hooks) == 2
    assert all(Path(p).read_bytes() in {base_mp4, cand_mp4} for p in verification.video_paths)
    replay = verification.replays[0]
    assert replay.baseline_video_url.startswith("/media/")
    assert replay.candidate_video_url.startswith("/media/")
    assert verification.bug_outcomes[0]["baseline_video_url"] == replay.baseline_video_url
    assert verification.bug_outcomes[0]["candidate_video_url"] == replay.candidate_video_url

    # Opening a kept PR with public_base_url links directly to baseline.mp4 and candidate.mp4.
    cand = Candidate(id="cand-mp4", hypothesis_id="h1", branch="pc/mp4", summary="cache deps", repo=repo)
    ev = verification.to_evidence(cand.id, (120.0,) * 5, (48.0,) * 5)
    verdict = rater.rate(ev)
    opened = bridge.open_kept_pr(
        cand,
        ev,
        verdict,
        verification.replays,
        config=ProofPRConfig(auto_open_pr=True, repo=repo),
    )
    assert opened is not None
    assert f"[baseline.mp4](https://pit-crew.example.test{replay.baseline_video_url})" in opened.body
    assert f"[candidate.mp4](https://pit-crew.example.test{replay.candidate_video_url})" in opened.body

    # Even if Gemini fails (unverified), persisted MP4 URLs are still kept on the Replay.
    bad_ask = partial(managed_agent.ask_multimodal, client=_FakeModelsClient(RuntimeError("gemini down")))
    unverified = bridge.verify_replay_runs(repo, [pair], ask=bad_ask)
    assert unverified.replays[0].status == "unverified"
    assert unverified.replays[0].baseline_video_url.startswith("/media/")
    assert unverified.replays[0].candidate_video_url.startswith("/media/")
    assert len(unverified.video_paths) == 2

    # Standalone MultimodalReplayVerifier also persists MP4 recordings when media_dir is configured.
    verifier_media = tmp_path / "verifier-media"
    verifier = MultimodalReplayVerifier(github, ask=ask, media_dir=verifier_media)
    standalone = verifier.verify_replay_runs(repo, [pair])
    assert standalone.bugs_caught == 1
    assert len(standalone.video_paths) == 2
    assert standalone.replays[0].baseline_video_url.startswith("/media/")
    assert standalone.replays[0].candidate_video_url.startswith("/media/")
