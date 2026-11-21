"""``pit-crew seed-target``: provisioning, baseline dispatch/await, idempotent resume, frozen workflow."""

import base64
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, override

import pytest

from fake_github import FakeGitHubClient, FakeRun
from pit_crew import benchmark_seeder as bs
from pit_crew import cli

REPO = "bench/target"
TEMPLATE_FILES = bs.load_template()
FROZEN = bs.git_blob_sha(TEMPLATE_FILES[bs.WORKFLOW_PATH])


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _seed(gh: FakeGitHubClient, **kwargs: Any) -> bs.SeedReport:  # noqa: ANN401
    clock = _Clock()
    options: dict[str, Any] = {"poll_seconds": 1.0, "timeout_seconds": 1000.0, "sleep": clock.sleep, "clock": clock}
    return bs.seed_target(gh, REPO, **{**options, **kwargs})


def _c0(gh: FakeGitHubClient) -> str:
    report = bs.provision(gh, REPO, TEMPLATE_FILES)
    return report[1]


# --- template ----------------------------------------------------------------------------------


def test_template_includes_workflow_and_skips_local_clutter(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"x")
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "stale.pyc").write_bytes(b"x")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    assert list(bs.load_template(tmp_path)) == [".github/workflows/ci.yml", ".gitignore", "pkg/m.py"]


def test_real_template_has_frozen_workflow_and_app() -> None:
    assert TEMPLATE_FILES[bs.WORKFLOW_PATH] == (bs.DEFAULT_TEMPLATE / bs.WORKFLOW_PATH).read_bytes()
    assert "calibration.json" in TEMPLATE_FILES
    assert "status_board/model.py" in TEMPLATE_FILES
    assert not [p for p in TEMPLATE_FILES if "__pycache__" in p]


def test_template_without_workflow_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(bs.SeedError, match=r"no \.github/workflows/ci\.yml"):
        bs.load_template(tmp_path)


def test_git_blob_sha_matches_git() -> None:
    assert bs.git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert bs.git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


# --- provisioning ------------------------------------------------------------------------------


def test_provision_creates_missing_repo_with_c0() -> None:
    gh = FakeGitHubClient()
    action, head = bs.provision(gh, REPO, TEMPLATE_FILES, private=True)
    assert action == "created"
    commit = gh.repos[REPO].commits[head]
    assert commit.parents == []
    assert commit.message == bs.C0_MESSAGE
    assert commit.files == TEMPLATE_FILES
    assert gh.repos[REPO].private
    assert gh.get_default_branch(REPO) == "main"
    assert gh.get_file_sha(REPO, bs.WORKFLOW_PATH, "main") == FROZEN


def test_provision_resets_repo_with_other_content() -> None:
    gh = FakeGitHubClient()
    old = gh.add_repo(REPO, {bs.WORKFLOW_PATH: b"on: push\n", "junk.txt": b"x"})
    action, head = bs.provision(gh, REPO, TEMPLATE_FILES)
    assert action == "reset"
    assert head != old
    assert gh.repos[REPO].commits[head].parents == []
    assert gh.repos[REPO].commits[head].files == TEMPLATE_FILES


def test_provision_resets_repo_with_wrong_default_branch() -> None:
    gh = FakeGitHubClient()
    gh.add_repo(REPO, branch="master")
    action, _ = bs.provision(gh, REPO, TEMPLATE_FILES)
    assert action == "reset"
    assert gh.get_default_branch(REPO) == "main"


def test_provision_leaves_already_frozen_main_alone_even_with_later_commits() -> None:
    gh = FakeGitHubClient()
    c0 = _c0(gh)
    c1 = bs.commit_on_main(gh, REPO, {"status_board/model.py": b"# bug\n"}, "c1: bug")
    writes = len(gh.writes)
    action, head = bs.provision(gh, REPO, TEMPLATE_FILES)
    assert (action, head) == ("existing", c1)
    assert gh.writes[writes:] == []
    assert gh.repos[REPO].commits[c1].parents == [c0]


def test_provision_raises_when_main_does_not_end_up_frozen() -> None:
    class Drifting(FakeGitHubClient):
        @override
        def create_commit(self, repo: str, files: Mapping[str, bytes], message: str, parents: Sequence[str]) -> str:
            return super().create_commit(repo, {**files, bs.WORKFLOW_PATH: b"on: push\n"}, message, parents)

    with pytest.raises(bs.WorkflowDriftError, match="expected frozen blob"):
        bs.provision(Drifting(), REPO, TEMPLATE_FILES)


def test_commit_on_main_refuses_to_touch_github_dir() -> None:
    gh = FakeGitHubClient()
    _c0(gh)
    with pytest.raises(bs.FrozenWorkflowEditError, match=r"\.github/workflows/ci\.yml"):
        bs.commit_on_main(gh, REPO, {bs.WORKFLOW_PATH: b"on: push\n"}, "nope")
    with pytest.raises(bs.FrozenWorkflowEditError):
        bs.commit_on_main(gh, REPO, {".github/dependabot.yml": b"x"}, "nope")
    assert gh.get_file_sha(REPO, bs.WORKFLOW_PATH, "main") == FROZEN


def test_commit_on_main_needs_a_seeded_main() -> None:
    gh = FakeGitHubClient()
    gh.add_repo(REPO, branch="master")
    with pytest.raises(bs.SeedError, match="run seed-target first"):
        bs.commit_on_main(gh, REPO, {"a.py": b""}, "c1")


# --- baseline ----------------------------------------------------------------------------------


def test_seed_from_scratch_dispatches_and_awaits_n_green_runs() -> None:
    gh = FakeGitHubClient()
    report = _seed(gh, baseline_runs=10, max_concurrent=3)
    assert report.provisioning == "created"
    assert report.workflow_sha == FROZEN
    assert len(report.green_runs) == 10
    assert report.dispatched == 10
    assert len(gh.writes_of("dispatch")) == 10
    assert all(w == ("dispatch", REPO, "ci.yml", "main") for w in gh.writes_of("dispatch"))
    runs = {r.id: r for r in gh.repos[REPO].runs}
    for run_id in report.green_runs:
        run = runs[run_id]
        assert (run.event, run.head_branch, run.conclusion) == ("workflow_dispatch", "main", "success")
        assert gh.get_file_sha(REPO, bs.WORKFLOW_PATH, run.head_sha) == FROZEN


def test_default_baseline_is_ten_runs() -> None:
    gh = FakeGitHubClient()
    assert len(_seed(gh).green_runs) == bs.DEFAULT_BASELINE_RUNS == 10


def test_never_more_than_max_concurrent_runs_in_flight() -> None:
    peak = 0

    class Watching(FakeGitHubClient):
        @override
        def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
            nonlocal peak
            super().dispatch_workflow(repo, workflow, ref)
            pending = len(self._hidden) + sum(r.status != "completed" for r in self.repos[repo].runs)
            peak = max(peak, pending)

    gh = Watching(complete_after=3)
    _seed(gh, baseline_runs=6, max_concurrent=2)
    assert peak == 2


def test_runs_slow_to_appear_are_not_redispatched() -> None:
    gh = FakeGitHubClient(visible_after=4, complete_after=2)
    report = _seed(gh, baseline_runs=3, max_concurrent=5)
    assert report.dispatched == 3
    assert len(report.green_runs) == 3


def test_seed_does_not_touch_github_dir_after_c0() -> None:
    gh = FakeGitHubClient()
    gh.add_repo(REPO, {bs.WORKFLOW_PATH: b"on: push\n"})
    _seed(gh, baseline_runs=3)
    _seed(gh, baseline_runs=5)
    touching = gh.commits_touching(REPO, bs.WORKFLOW_DIR)
    assert [c.message for c in touching] == [bs.C0_MESSAGE]
    assert gh.get_file_sha(REPO, bs.WORKFLOW_PATH, "main") == FROZEN


def test_only_frozen_green_dispatch_runs_on_main_count() -> None:
    gh = FakeGitHubClient(filter_runs=False)  # the seeder must filter on its own
    c0 = _c0(gh)
    stale = gh.add_repo("other/x")  # a SHA the target repo does not know (e.g. from before a reset)
    gh.add_run(REPO, c0, event="push")
    gh.add_run(REPO, c0, branch="feature")
    gh.add_run(REPO, c0, path=".github/workflows/other.yml")
    gh.add_run(REPO, stale)
    gh.add_run(REPO, c0, conclusion="failure")
    counted = gh.add_run(REPO, c0)
    report = _seed(gh, baseline_runs=2)
    assert counted.id in report.green_runs
    assert report.dispatched == 1


def test_green_runs_on_an_older_workflow_do_not_count_after_reset() -> None:
    gh = FakeGitHubClient()
    old = gh.add_repo(REPO, {bs.WORKFLOW_PATH: b"on: workflow_dispatch\n"})
    stale = {gh.add_run(REPO, old).id for _ in range(5)}
    report = _seed(gh, baseline_runs=3)
    assert report.provisioning == "reset"
    assert report.dispatched == 3
    assert not stale.intersection(report.green_runs)


def test_runs_listed_with_versioned_path_still_count() -> None:
    gh = FakeGitHubClient()
    c0 = _c0(gh)
    run = gh.add_run(REPO, c0, path=f"{bs.WORKFLOW_PATH}@refs/heads/main")
    report = _seed(gh, baseline_runs=1)
    assert (report.green_runs, report.dispatched) == ([run.id], 0)


def test_red_baseline_fails_after_max_failures() -> None:
    gh = FakeGitHubClient(outcome=lambda _run: "failure")
    with pytest.raises(bs.BaselineFailedError, match="must be green"):
        _seed(gh, baseline_runs=5, max_concurrent=1)
    assert len(gh.writes_of("dispatch")) == 3


def test_baseline_times_out() -> None:
    gh = FakeGitHubClient(complete_after=10_000)
    with pytest.raises(bs.BaselineTimeoutError, match="0/2 green"):
        _seed(gh, baseline_runs=2, timeout_seconds=5.0)
    assert len(gh.writes_of("dispatch")) == 2


def test_flaky_red_runs_are_tolerated_below_the_cap() -> None:
    def every_third_fails(run: FakeRun) -> str:
        return "failure" if run.id % 3 == 0 else "success"

    gh = FakeGitHubClient(outcome=every_third_fails)
    report = _seed(gh, baseline_runs=4, max_concurrent=1)
    assert len(report.green_runs) == 4
    assert report.dispatched > 4


def test_drift_during_baseline_is_detected() -> None:
    class Sneaky(FakeGitHubClient):
        @override
        def dispatch_workflow(self, repo: str, workflow: str, ref: str) -> None:
            super().dispatch_workflow(repo, workflow, ref)
            sha = self.create_commit(repo, {bs.WORKFLOW_PATH: b"on: push\n"}, "edit", [self.repos[repo].refs["main"]])
            self.repos[repo].refs["main"] = sha

    with pytest.raises(bs.WorkflowDriftError):
        _seed(Sneaky(), baseline_runs=1)


def test_bad_arguments_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        _seed(FakeGitHubClient(), baseline_runs=0)


# --- idempotent resume -------------------------------------------------------------------------


def test_rerun_on_fully_seeded_repo_writes_nothing() -> None:
    gh = FakeGitHubClient()
    first = _seed(gh, baseline_runs=4)
    writes = len(gh.writes)
    second = _seed(gh, baseline_runs=4)
    assert gh.writes[writes:] == []
    assert second.provisioning == "existing"
    assert second.dispatched == 0
    assert second.green_runs == first.green_runs
    assert second.head_sha == first.head_sha


def test_rerun_on_partially_seeded_repo_dispatches_only_the_shortfall() -> None:
    gh = FakeGitHubClient()
    c0 = _c0(gh)
    for _ in range(3):
        gh.add_run(REPO, c0)
    report = _seed(gh, baseline_runs=10)
    assert report.provisioning == "existing"
    assert report.dispatched == 7
    assert len(report.green_runs) == 10
    assert len(gh.writes_of("create_commit")) == 1  # only the first provision's c0


def test_rerun_counts_runs_still_in_flight_from_an_interrupted_seed() -> None:
    gh = FakeGitHubClient()
    c0 = _c0(gh)
    gh.add_run(REPO, c0)
    gh.add_run(REPO, c0, conclusion=None)
    gh.add_run(REPO, c0, conclusion=None)
    report = _seed(gh, baseline_runs=4)
    assert report.dispatched == 1
    assert len(report.green_runs) == 4


def test_raising_the_baseline_extends_it() -> None:
    gh = FakeGitHubClient()
    _seed(gh, baseline_runs=2)
    report = _seed(gh, baseline_runs=5)
    assert report.dispatched == 3
    assert len(report.green_runs) == 5


def test_runs_on_later_frozen_commits_count() -> None:
    gh = FakeGitHubClient()
    _c0(gh)
    c1 = bs.commit_on_main(gh, REPO, {"status_board/extra.py": b"x = 1\n"}, "c1")
    gh.add_run(REPO, c1)
    report = _seed(gh, baseline_runs=1)
    assert report.dispatched == 0
    assert report.head_sha == c1


# --- REST client -------------------------------------------------------------------------------


class _Transport:
    """Answers REST calls from a small in-memory GitHub; records every request."""

    def __init__(self, *, user: str = "bench", exists: bool = True) -> None:
        self.user = user
        self.exists = exists
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.refs: dict[str, str] = {}
        self.files: dict[str, str] = {}

    def __call__(self, method: str, url: str, body: Mapping[str, Any] | None) -> tuple[int, Any]:  # noqa: C901, PLR0911
        self.calls.append((method, url, body))
        path = url.removeprefix(f"{bs.API}/repos/{REPO}")
        if url == f"{bs.API}/user":
            return 200, {"login": self.user}
        if method == "POST" and url.endswith("/repos") and body is not None:
            self.exists = True
            return 201, {}
        if not self.exists:
            return 404, {"message": "Not Found"}
        if path == "":
            return (200, {"default_branch": "main"}) if method == "GET" else (200, {})
        if path.startswith("/git/ref/heads/"):
            sha = self.refs.get(path.removeprefix("/git/ref/heads/"))
            return (200, {"object": {"sha": sha}}) if sha else (404, {})
        if path.startswith("/contents/"):
            sha = self.files.get(path.removeprefix("/contents/").split("?")[0])
            return (200, {"sha": sha}) if sha else (404, {})
        if path == "/git/blobs" and body is not None:
            return 201, {"sha": bs.git_blob_sha(base64.b64decode(body["content"]))}
        if path.startswith("/git/commits/"):
            return 200, {"tree": {"sha": "basetree"}}
        if path in {"/git/trees", "/git/commits"}:
            return 201, {"sha": "t" if path == "/git/trees" else "c"}
        if method in {"POST", "PATCH"} and "/git/refs" in path:
            return 201, {}
        if path.endswith("/dispatches"):
            return 204, None
        if "/runs?" in path:
            run = {"id": 7, "head_sha": "c", "head_branch": "main", "event": "workflow_dispatch"}
            return 200, {"workflow_runs": [{**run, "path": bs.WORKFLOW_PATH, "status": "queued", "conclusion": None}]}
        return 500, {"message": f"unexpected {method} {path}"}


def test_rest_client_reads_and_handles_missing() -> None:
    t = _Transport(exists=False)
    client = bs.RestGitHubClient(t)
    assert client.get_default_branch(REPO) is None
    t.exists = True
    t.refs["main"] = "abc"
    t.files[".github/workflows/ci.yml"] = FROZEN
    assert client.get_default_branch(REPO) == "main"
    assert client.get_ref(REPO, "main") == "abc"
    assert client.get_ref(REPO, "gone") is None
    assert client.get_file_sha(REPO, bs.WORKFLOW_PATH, "abc") == FROZEN
    assert client.get_file_sha(REPO, "missing.txt", "abc") is None
    (run,) = client.list_workflow_runs(REPO, "ci.yml", branch="main", event="workflow_dispatch")
    assert (run.id, run.status, run.green) == (7, "queued", False)
    assert "branch=main&event=workflow_dispatch" in t.calls[-1][1]


def test_rest_client_creates_repo_under_user_or_org() -> None:
    t = _Transport(user="bench")
    bs.RestGitHubClient(t).create_repo(REPO, private=True)
    assert t.calls[-1] == ("POST", f"{bs.API}/user/repos", {"name": "target", "private": True, "auto_init": True})
    t = _Transport(user="someone-else")
    bs.RestGitHubClient(t).create_repo(REPO, private=False)
    assert t.calls[-1][1] == f"{bs.API}/orgs/bench/repos"


def test_rest_client_commits_byte_exact_blobs_and_moves_refs() -> None:
    t = _Transport()
    client = bs.RestGitHubClient(t)
    files = {bs.WORKFLOW_PATH: TEMPLATE_FILES[bs.WORKFLOW_PATH], "a.txt": b"\x00\xff"}
    assert client.create_commit(REPO, files, bs.C0_MESSAGE, []) == "c"
    tree_call = next(c for c in t.calls if c[1].endswith("/git/trees"))
    assert tree_call[2] is not None
    entries = {e["path"]: e["sha"] for e in tree_call[2]["tree"]}
    assert entries[bs.WORKFLOW_PATH] == FROZEN
    assert entries["a.txt"] == bs.git_blob_sha(b"\x00\xff")
    assert "base_tree" not in tree_call[2]
    assert t.calls[-1][2] == {"message": bs.C0_MESSAGE, "tree": "t", "parents": []}

    client.create_commit(REPO, {"b.txt": b"b"}, "c1", ["c"])
    tree_call = [c for c in t.calls if c[1].endswith("/git/trees")][-1]
    assert tree_call[2] is not None
    assert tree_call[2]["base_tree"] == "basetree"

    client.set_ref(REPO, "main", "c")
    assert t.calls[-1][:2] == ("POST", f"{bs.API}/repos/{REPO}/git/refs")
    t.refs["main"] = "old"
    client.set_ref(REPO, "main", "c")
    assert t.calls[-1] == ("PATCH", f"{bs.API}/repos/{REPO}/git/refs/heads/main", {"sha": "c", "force": True})
    client.set_default_branch(REPO, "main")
    assert t.calls[-1] == ("PATCH", f"{bs.API}/repos/{REPO}", {"default_branch": "main"})
    client.dispatch_workflow(REPO, "ci.yml", "main")
    assert t.calls[-1] == ("POST", f"{bs.API}/repos/{REPO}/actions/workflows/ci.yml/dispatches", {"ref": "main"})


def test_rest_client_raises_on_errors() -> None:
    client = bs.RestGitHubClient(lambda _m, _u, _b: (403, {"message": "Resource not accessible"}))
    with pytest.raises(bs.GitHubAPIError, match="403"):
        client.dispatch_workflow(REPO, "ci.yml", "main")


def test_urllib_transport_refuses_non_github_urls() -> None:
    send = bs.urllib_transport("secret")
    with pytest.raises(ValueError, match=r"not under https://api\.github\.com"):
        send("GET", "https://example.com/repos/x", None)


# --- CLI ---------------------------------------------------------------------------------------


def _cli(argv: list[str], gh: FakeGitHubClient | None, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    code = cli.main(["seed-target", "--repo", REPO, "--poll-seconds", "0", *argv], github=gh)
    return code, json.loads(capsys.readouterr().out)


def test_cli_seed_target_seeds_and_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHubClient()
    code, out = _cli(["--baseline-runs", "3"], gh, capsys)
    assert code == 0
    assert out["status"] == "seeded"
    assert out["provisioning"] == "created"
    assert out["workflow_sha"] == FROZEN
    assert len(out["green_runs"]) == 3
    code, out = _cli(["--baseline-runs", "3"], gh, capsys)
    assert (code, out["provisioning"], out["dispatched"]) == (0, "existing", 0)


def test_cli_seed_target_reports_failure_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHubClient(outcome=lambda _run: "failure")
    code, out = _cli(["--baseline-runs", "1"], gh, capsys)
    assert code == 1
    assert out["status"] == "error"
    assert "BaselineFailedError" in out["error"]


def test_cli_seed_target_needs_a_token(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code, out = _cli([], None, capsys)
    assert code == 1
    assert "GITHUB_TOKEN" in out["error"]


def test_cli_seed_target_builds_rest_client_from_token(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []

    def fake_transport(token: str) -> bs.Transport:
        built.append(token)
        return lambda _m, _u, _b: (401, {"message": "Bad credentials"})

    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(bs, "urllib_transport", fake_transport)
    code, out = _cli([], None, capsys)
    assert (code, built) == (1, ["tok"])
    assert "401" in out["error"]


@pytest.mark.parametrize("argv", [["--repo", "noslash"], ["--repo", "a/b/c"], ["--repo", REPO, "--baseline-runs", "0"]])
def test_cli_seed_target_validates_flags(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["seed-target", *argv])
