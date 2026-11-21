import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pit_crew.contracts import Candidate, Hypothesis, Verdict
from pit_crew.ledger import Ledger, SqliteLedger
from pit_crew.publisher import (
    DisallowedCandidatePathError,
    UnpublishableCandidateError,
    commit_message,
    publish_candidate,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pit_crew.github import CandidatePublisherClient, GitHubClient
    from pit_crew.lanes.proof.miner import JobRun

REPO = "org/fork"
BASE = "base000"
WORKFLOW = ".github/workflows/ci.yml"


class FakeGitHub:
    """In-memory branches and commits. Each commit's sha hashes its parent, files and message."""

    def __init__(self) -> None:
        self.branches: dict[tuple[str, str], str] = {}
        self.commits: dict[str, tuple[str, dict[str, str], str]] = {}
        self.calls: list[tuple[str, ...]] = []

    def reset_branch(self, repo: str, branch: str, sha: str) -> None:
        self.calls.append(("reset_branch", repo, branch, sha))
        self.branches[repo, branch] = sha

    def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        self.calls.append(("commit_files", repo, branch))
        parent = self.branches[repo, branch]
        digest = hashlib.sha1(f"{parent}{sorted(files.items())}{message}".encode(), usedforsecurity=False)
        sha = digest.hexdigest()
        self.commits[sha] = (parent, dict(files), message)
        self.branches[repo, branch] = sha
        return sha

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        self.calls.append(("get_file_content", repo, ref, path))
        commit = self.commits.get(ref)
        return commit[1].get(path) if commit is not None else None

    def list_workflow_job_runs(self, repo: str) -> "Sequence[JobRun]":
        self.calls.append(("list_workflow_job_runs", repo))
        return ()

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        self.calls.append(("get_run_failure_output", repo, run_id))
        return b""

    def open_pull_request(self, repo: str, *, head: str, base: str, title: str, body: str) -> str:
        self.calls.append(("open_pull_request", repo, head, base, title, body))
        return f"https://github.com/{repo}/pull/1"


@pytest.fixture
def gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def ledger(tmp_path: Path) -> SqliteLedger:
    return SqliteLedger(tmp_path / "pit_crew.db")


def _candidate(**overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "hypothesis_id": "hyp-1",
        "branch": "pit-crew/dep-cache",
        "summary": "Cache uv downloads in CI",
        "repo": REPO,
        "files": {WORKFLOW: "name: ci\n"},
    }
    fields.update(overrides)
    return Candidate(**fields)  # type: ignore[arg-type]


def test_fake_satisfies_protocols(gh: FakeGitHub) -> None:
    client: GitHubClient = gh
    publisher_client: CandidatePublisherClient = client
    assert publisher_client is gh


def test_publishes_files_onto_branch_from_resolved_base(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    cand = _candidate(base_sha="stale-sha")

    published = publish_candidate(gh, ledger, cand, BASE)

    assert gh.calls == [
        ("reset_branch", REPO, cand.branch, BASE),
        ("commit_files", REPO, cand.branch),
    ]
    assert published.head_sha == gh.branches[REPO, cand.branch]
    assert published.base_sha == BASE
    parent, files, message = gh.commits[published.head_sha]
    assert parent == BASE
    assert files == cand.files
    assert message == commit_message(cand)
    assert published.id == cand.id
    assert published.files == cand.files
    assert published.changed_files == (WORKFLOW,)


def test_persists_published_candidate_in_sqlite(gh: FakeGitHub, ledger: SqliteLedger, tmp_path: Path) -> None:
    cand = _candidate()
    ledger.add(cand)  # Optimizer's unpublished record, head_sha=""

    published = publish_candidate(gh, ledger, cand, BASE)

    reopened = SqliteLedger(tmp_path / "pit_crew.db")
    assert reopened.get(Candidate, cand.id) == published
    assert [c.head_sha for c in reopened.all(Candidate)] == [published.head_sha]


def test_retry_force_resets_branch_to_base(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    cand = _candidate()
    gh.branches[REPO, cand.branch] = "leftover-from-failed-attempt"

    first = publish_candidate(gh, ledger, cand, BASE)
    second = publish_candidate(gh, ledger, cand, BASE)

    assert gh.commits[first.head_sha][0] == BASE
    assert gh.commits[second.head_sha][0] == BASE
    assert first.head_sha == second.head_sha
    assert len(ledger.all(Candidate)) == 1


@pytest.mark.parametrize(
    ("files", "changed_files", "bad"),
    [
        ({"src/app.py": "x"}, (), ("src/app.py",)),
        ({WORKFLOW: "x", "tests/test_x.py": "x"}, (), ("tests/test_x.py",)),
        ({WORKFLOW: "x"}, (WORKFLOW, "../escape.yml"), ("../escape.yml",)),
        ({}, ("lib/thing.js",), ("lib/thing.js",)),
    ],
)
def test_disallowed_paths_rejected_before_any_github_call(
    gh: FakeGitHub,
    ledger: SqliteLedger,
    files: dict[str, str],
    changed_files: tuple[str, ...],
    bad: tuple[str, ...],
) -> None:
    cand = _candidate(files=files, changed_files=changed_files)

    with pytest.raises(DisallowedCandidatePathError) as exc:
        publish_candidate(gh, ledger, cand, BASE)

    assert exc.value.paths == bad
    assert exc.value.candidate_id == cand.id
    assert gh.calls == []
    assert ledger.all(Candidate) == []


def test_disallowed_path_rejected_even_when_already_published(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    cand = _candidate(files={}, changed_files=("src/app.py",), head_sha="abc")

    with pytest.raises(DisallowedCandidatePathError):
        publish_candidate(gh, ledger, cand, BASE)
    assert gh.calls == []


def test_already_published_candidate_returned_unchanged(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    cand = _candidate(files={}, changed_files=(WORKFLOW,), head_sha="abc123", base_sha="b0")

    assert publish_candidate(gh, ledger, cand, BASE) is cand
    assert gh.calls == []
    assert ledger.all(Candidate) == []


def test_head_sha_with_files_is_republished(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    cand = _candidate(head_sha="old-head")

    published = publish_candidate(gh, ledger, cand, BASE)

    assert published.head_sha != "old-head"
    assert gh.commits[published.head_sha][0] == BASE


@pytest.mark.parametrize(
    ("overrides", "base", "what"),
    [
        ({"files": {}}, BASE, "files"),
        ({"repo": ""}, BASE, "repo"),
        ({"branch": ""}, BASE, "branch"),
        ({}, "", "resolved_base_sha"),
    ],
)
def test_unpublishable_candidate_raises_without_github_calls(
    gh: FakeGitHub, ledger: SqliteLedger, overrides: dict[str, object], base: str, what: str
) -> None:
    with pytest.raises(UnpublishableCandidateError, match=what):
        publish_candidate(gh, ledger, _candidate(**overrides), base)
    assert gh.calls == []


def test_commit_message_names_summary_files_and_ledger_ids() -> None:
    cand = _candidate(summary="Cache uv downloads\n\nlonger body", files={WORKFLOW: "a", "pyproject.toml": "b"})

    message = commit_message(cand)

    assert message.startswith("pit-crew: Cache uv downloads\n\n")
    assert f"- {WORKFLOW}\n- pyproject.toml" in message
    assert f"Pit-Crew-Candidate: {cand.id}" in message
    assert "Pit-Crew-Hypothesis: hyp-1" in message


def test_commit_message_falls_back_when_summary_blank() -> None:
    assert commit_message(_candidate(summary="  ")).startswith("pit-crew: CI speedup candidate\n")


def _keep(ledger: Ledger, cand: Candidate) -> None:
    ledger.add(Verdict(candidate_id=cand.id, keep=True, speedup=1.4, p_value=0.01, reason="faster"))


def _resolve_base(ledger: Ledger, repo: str, default: str) -> str:
    kept = ledger.latest_kept_candidate(repo)
    return kept.head_sha if kept else default


def test_compounding_cycles_advance_base_sha_atop_published_commits(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    hyp1 = Hypothesis(
        repo=REPO, description="no cache", skill="dep-cache", expected_savings_seconds=60.0, base_sha=BASE
    )
    hyp2 = Hypothesis(
        repo=REPO, description="serial jobs", skill="parallelize-dag", expected_savings_seconds=90.0, base_sha=BASE
    )
    ledger.add(hyp1)
    ledger.add(hyp2)
    assert ledger.latest_kept_candidate(REPO) is None

    # Cycle 1: nothing kept yet, so base is the repo default.
    c1 = _candidate(hypothesis_id=hyp1.id, branch="pit-crew/c1")
    ledger.add(c1)
    p1 = publish_candidate(gh, ledger, c1, _resolve_base(ledger, REPO, BASE))
    _keep(ledger, p1)

    kept = ledger.latest_kept_candidate(REPO)
    assert kept is not None
    assert kept.head_sha == p1.head_sha
    assert kept.base_sha == BASE

    # Cycle 2: base advances onto Cycle 1's published commit.
    c2 = _candidate(hypothesis_id=hyp2.id, branch="pit-crew/c2", files={"pyproject.toml": "[tool]\n"})
    ledger.add(c2)
    p2 = publish_candidate(gh, ledger, c2, _resolve_base(ledger, REPO, BASE))

    assert p2.base_sha == p1.head_sha
    assert gh.commits[p2.head_sha][0] == p1.head_sha
    _keep(ledger, p2)

    # Cycle 3 would build atop Cycle 2.
    latest = ledger.latest_kept_candidate(REPO)
    assert latest is not None
    assert latest.id == c2.id
    assert latest.base_sha == p1.head_sha
    assert latest.head_sha == p2.head_sha
    assert {c.id: c.head_sha for c in ledger.all(Candidate)} == {c1.id: p1.head_sha, c2.id: p2.head_sha}


def test_rejected_candidate_does_not_advance_base(gh: FakeGitHub, ledger: SqliteLedger) -> None:
    c1 = _candidate(branch="pit-crew/c1")
    p1 = publish_candidate(gh, ledger, c1, BASE)
    _keep(ledger, p1)

    c2 = _candidate(branch="pit-crew/c2", files={"pyproject.toml": "x"})
    p2 = publish_candidate(gh, ledger, c2, _resolve_base(ledger, REPO, BASE))
    ledger.add(Verdict(candidate_id=p2.id, keep=False, speedup=0.9, p_value=0.6, reason="slower"))

    assert _resolve_base(ledger, REPO, BASE) == p1.head_sha
