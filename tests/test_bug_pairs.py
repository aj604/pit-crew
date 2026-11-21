"""``seed-target``'s red-to-green bug pairs: order, frozen workflow, Proof's miner, idempotent resume."""

from collections.abc import Mapping
from typing import Any, override

import pytest
import yaml

from fake_github import CI_JOBS, TEMPLATE_CI, FakeGitHubClient
from pit_crew import benchmark_seeder as bs
from pit_crew.lanes.proof.miner import JobRun, red_commits

REPO = "bench/target"
TEMPLATE_FILES = bs.load_template()
FROZEN = bs.git_blob_sha(TEMPLATE_FILES[bs.WORKFLOW_PATH])
SEQUENCE = [m for p in bs.BUG_PAIRS for m in (p.bug_message, p.fix_message)]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _seed(gh: FakeGitHubClient, **kwargs: Any) -> bs.SeedReport:  # noqa: ANN401
    clock = _Clock()
    options: dict[str, Any] = {"poll_seconds": 1.0, "timeout_seconds": 100.0, "sleep": clock.sleep, "clock": clock}
    return bs.seed_target(gh, REPO, **{"baseline_runs": 2, **options, **kwargs})


def _after_c0(gh: FakeGitHubClient) -> list[tuple[str, str]]:
    history = gh.list_commits(REPO, "main")
    messages = [m for _, m in history]
    return list(reversed(history[: messages.index(bs.C0_MESSAGE)]))


# --- the pairs themselves ----------------------------------------------------------------------


def test_one_pair_per_check_in_order_matching_the_workflow() -> None:
    jobs = yaml.safe_load(TEMPLATE_FILES[bs.WORKFLOW_PATH])["jobs"]
    assert [p.key for p in bs.BUG_PAIRS] == ["b1", "b2", "b3", "b4"]
    assert [p.job for p in bs.BUG_PAIRS] == ["lint", "typecheck", "unit-test", "ui-check"]
    for pair in bs.BUG_PAIRS:
        assert jobs[pair.job]["name"] == pair.job_name
        assert not pair.path.startswith(bs.WORKFLOW_DIR)
        assert TEMPLATE_CI[pair.key][pair.job_name] == "failure"


def test_bug_changes_one_file_and_fix_restores_it() -> None:
    for pair in bs.BUG_PAIRS:
        bug = pair.bug_files(TEMPLATE_FILES)
        assert list(bug) == [pair.path]
        assert bug[pair.path] != TEMPLATE_FILES[pair.path]
        assert pair.fix_files(TEMPLATE_FILES) == {pair.path: TEMPLATE_FILES[pair.path]}


def test_patch_anchor_must_occur_exactly_once() -> None:
    pair = bs.BUG_PAIRS[0]
    doubled = {**TEMPLATE_FILES, pair.path: TEMPLATE_FILES[pair.path] + pair.old.encode()}
    with pytest.raises(bs.SeedError, match="exactly one"):
        pair.bug_files(doubled)
    missing = {k: v for k, v in TEMPLATE_FILES.items() if k != pair.path}
    with pytest.raises(bs.SeedError, match="has no"):
        pair.bug_files(missing)


# --- seeding -----------------------------------------------------------------------------------


def test_seeds_all_pairs_in_order_before_the_baseline() -> None:
    gh = FakeGitHubClient()
    report = _seed(gh)
    assert [m for _, m in _after_c0(gh)] == SEQUENCE
    assert [p.key for p in report.bug_pairs] == ["b1", "b2", "b3", "b4"]
    assert all(p.created for p in report.bug_pairs)
    assert report.head_sha == report.bug_pairs[-1].fix_sha == gh.get_ref(REPO, "main")
    last_pair_write = max(i for i, w in enumerate(gh.writes) if w[0] == "set_ref")
    first_dispatch = min(i for i, w in enumerate(gh.writes) if w[0] == "dispatch")
    assert last_pair_write < first_dispatch
    runs = {r.id: r for r in gh.repos[REPO].runs}
    for pair in report.bug_pairs:
        bug_run, fix_run = runs[pair.bug_run], runs[pair.fix_run]
        assert (bug_run.head_sha, bug_run.event, bug_run.conclusion) == (pair.bug_sha, "push", "failure")
        assert (fix_run.head_sha, fix_run.event, fix_run.conclusion) == (pair.fix_sha, "push", "success")
    assert report.as_json()["bug_pairs"][2]["job"] == "unit-test"


def test_workflow_blob_is_frozen_across_every_bug_and_fix() -> None:
    gh = FakeGitHubClient()
    report = _seed(gh)
    c0 = gh.list_commits(REPO, "main")[-1][0]
    c0_files = gh.repos[REPO].commits[c0].files
    assert [c.sha for c in gh.commits_touching(REPO, bs.WORKFLOW_DIR)] == [c0]
    for pair, step in zip(bs.BUG_PAIRS, report.bug_pairs, strict=True):
        bug_files = gh.repos[REPO].commits[step.bug_sha].files
        assert gh.get_file_sha(REPO, bs.WORKFLOW_PATH, step.bug_sha) == FROZEN
        assert gh.get_file_sha(REPO, bs.WORKFLOW_PATH, step.fix_sha) == FROZEN
        assert {p for p in bug_files if bug_files[p] != c0_files.get(p)} == {pair.path}
        assert gh.repos[REPO].commits[step.fix_sha].files == c0_files
    assert report.workflow_sha == FROZEN


def _job_rows(gh: FakeGitHubClient) -> list[JobRun]:
    """Flatten the fake's runs into Proof's miner input, one row per job, in run order."""
    return [
        JobRun(
            sha=run.head_sha,
            branch=run.head_branch,
            created_at=f"2026-11-21T00:00:{i:02d}Z",
            workflow="CI",
            job=job,
            attempt=1,
            conclusion=conclusion,
        )
        for i, run in enumerate(sorted(gh.repos[REPO].runs, key=lambda r: r.id))
        for job, conclusion in (run.jobs or dict.fromkeys(CI_JOBS, run.conclusion or "")).items()
    ]


def test_proof_miner_classifies_all_four_checks_as_fixed() -> None:
    gh = FakeGitHubClient()
    report = _seed(gh)
    red = {c.sha: c for c in red_commits(_job_rows(gh))}
    assert set(red) == {p.bug_sha for p in report.bug_pairs}
    for pair, step in zip(bs.BUG_PAIRS, report.bug_pairs, strict=True):
        assert f"CI/{pair.job_name}" in red[step.bug_sha].checks
        assert red[step.bug_sha].fixed
    caught = {check for c in red.values() for check in c.checks}
    assert {"CI/Lint", "CI/Typecheck", "CI/Unit test", "CI/UI check"} <= caught


def test_push_runs_slow_to_appear_are_awaited() -> None:
    gh = FakeGitHubClient(visible_after=3, complete_after=2)
    report = _seed(gh)
    assert len(report.bug_pairs) == 4
    assert len(gh.writes_of("create_commit")) == 1 + 8


# --- idempotent resume -------------------------------------------------------------------------


def test_rerun_on_fully_seeded_repo_writes_nothing() -> None:
    gh = FakeGitHubClient()
    first = _seed(gh)
    writes = len(gh.writes)
    second = _seed(gh)
    assert gh.writes[writes:] == []
    assert [(p.bug_sha, p.fix_sha, p.bug_run, p.fix_run) for p in second.bug_pairs] == [
        (p.bug_sha, p.fix_sha, p.bug_run, p.fix_run) for p in first.bug_pairs
    ]
    assert not any(p.created for p in second.bug_pairs)


def test_resume_after_some_pairs_seeds_only_the_rest() -> None:
    gh = FakeGitHubClient()
    _seed(gh, bug_pairs=bs.BUG_PAIRS[:2])
    commits = len(gh.writes_of("create_commit"))
    report = _seed(gh)
    assert len(gh.writes_of("create_commit")) - commits == 4
    assert [p.created for p in report.bug_pairs] == [False, False, True, True]
    assert [m for _, m in _after_c0(gh)] == SEQUENCE


def test_resume_between_a_bug_and_its_fix() -> None:
    gh = FakeGitHubClient()
    bs.provision(gh, REPO, TEMPLATE_FILES)
    pair = bs.BUG_PAIRS[0]
    bug = bs.commit_on_main(gh, REPO, pair.bug_files(TEMPLATE_FILES), pair.bug_message)
    commits = len(gh.writes_of("create_commit"))
    report = _seed(gh)
    assert report.bug_pairs[0].bug_sha == bug
    assert report.bug_pairs[0].created
    assert len(gh.writes_of("create_commit")) - commits == 7
    assert [m for _, m in _after_c0(gh)] == SEQUENCE


def test_foreign_commits_after_c0_are_refused() -> None:
    gh = FakeGitHubClient()
    bs.provision(gh, REPO, TEMPLATE_FILES)
    bs.commit_on_main(gh, REPO, {"notes.txt": b"x"}, "something else")
    with pytest.raises(bs.SeedError, match="not a prefix"):
        _seed(gh)
    gh = FakeGitHubClient()
    _seed(gh, bug_pairs=bs.BUG_PAIRS[:1])
    with pytest.raises(bs.SeedError, match="not a prefix"):
        _seed(gh, bug_pairs=bs.BUG_PAIRS[1:])


def test_missing_c0_in_history_is_refused() -> None:
    class Amnesiac(FakeGitHubClient):
        @override
        def list_commits(self, repo: str, branch: str) -> list[tuple[str, str]]:
            return super().list_commits(repo, branch)[:-1]

    with pytest.raises(bs.SeedError, match="cannot find"):
        _seed(Amnesiac())


# --- runs that do not go the way a pair needs ---------------------------------------------------


def _ci(jobs: Mapping[str, str]) -> Any:  # noqa: ANN401
    return lambda _files: dict(jobs)


def test_bug_that_does_not_break_its_check_is_an_error() -> None:
    gh = FakeGitHubClient(ci=_ci(TEMPLATE_CI["clean"]))
    with pytest.raises(bs.BugPairError, match=r"should fail 'Lint'.*'success'"):
        _seed(gh)


def test_bug_that_breaks_a_different_check_is_an_error() -> None:
    gh = FakeGitHubClient(ci=_ci(TEMPLATE_CI["b2"]))
    with pytest.raises(bs.BugPairError, match=r"should fail 'Lint'.*concluded 'failure'"):
        _seed(gh)


def test_fix_that_stays_red_is_an_error() -> None:
    gh = FakeGitHubClient(ci=_ci(TEMPLATE_CI["b1"]))
    with pytest.raises(bs.BugPairError, match=r"f1: .*should be green"):
        _seed(gh)


def test_missing_push_run_times_out() -> None:
    gh = FakeGitHubClient(ci=None)
    with pytest.raises(bs.BugPairTimeoutError, match="b1: "):
        _seed(gh)
    assert gh.writes_of("dispatch") == []
