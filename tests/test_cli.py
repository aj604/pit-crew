import gzip
import io
import json
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pit_crew import cli, managed_agent
from pit_crew import workspace as host_workspace
from pit_crew.contracts import Candidate, Hypothesis, ReplayPlan
from pit_crew.lanes.analysis import history
from pit_crew.lanes.analysis import tools as analysis_tools
from pit_crew.ledger import SqliteLedger

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Analysis smoke runs need a local checkout; default every test to an empty temp one."""
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path))


@dataclass
class _FakeStep:
    type: str
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] | None = None


@dataclass
class _FakeInteraction:
    id: str
    status: str | None
    steps: list[_FakeStep] | None = None
    continuation_token: str | None = None


@dataclass
class _FakeInteractionsAPI:
    responses: list[_FakeInteraction]
    calls: list[dict[str, object]] = field(default_factory=list)

    def create(self, **kwargs: object) -> _FakeInteraction:
        self.calls.append(kwargs)
        return self.responses.pop(0)

    def get(self, interaction_id: str, /) -> _FakeInteraction:
        raise AssertionError(interaction_id)


class _FakeClient:
    def __init__(self, *responses: _FakeInteraction) -> None:
        self.interactions = _FakeInteractionsAPI(list(responses))


def _calling(tool: str, arguments: dict[str, Any], final_status: str = "completed") -> _FakeClient:
    return _FakeClient(
        _FakeInteraction(
            id="int-1",
            status="requires_action",
            steps=[_FakeStep(type="function_call", id="call-1", name=tool, arguments=arguments)],
        ),
        _FakeInteraction(id="int-2", status=final_status),
    )


def _hypothesis_args(repo: str = "org/fork") -> dict[str, Any]:
    return {"repo": repo, "description": "no cache", "skill": "dep-cache", "expected_savings_seconds": 60.0}


def _run(argv: list[str], client: _FakeClient, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = cli.main(argv, client=client)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    return code, json.loads(out[0])


def _argv(lane: str, ledger: Path, *extra: str) -> list[str]:
    return ["smoke-lane", "--lane", lane, "--repo", "org/fork", "--ledger", str(ledger), *extra]


def test_analysis_success_prints_summary_and_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "ledger.db"
    client = _calling("record_hypothesis", _hypothesis_args())

    code, summary = _run(_argv("analysis", ledger_path), client, capsys)

    assert code == 0
    assert summary["interaction_id"] == "int-2"
    assert summary["status"] == "completed"
    assert summary["records_written"] == 1
    assert summary["expected_record"] == "Hypothesis"
    assert isinstance(summary["elapsed_seconds"], float)
    assert len(SqliteLedger(ledger_path).all(Hypothesis)) == 1

    request = client.interactions.calls[0]
    assert (
        request["input"] == f"Lane: analysis\n\n{cli.default_task('analysis', 'org/fork', SqliteLedger(ledger_path))}"
    )
    assert "record_hypothesis" in str(request["input"])
    sources = request["environment"]["sources"]  # type: ignore[index]
    assert {"type": "repository", "source": "https://github.com/org/fork", "target": "/workspace"} in sources


def test_task_override_replaces_default_prompt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = _calling("record_hypothesis", _hypothesis_args())

    code, _ = _run(_argv("analysis", tmp_path / "l.db", "--task", "custom prompt"), client, capsys)

    assert code == 0
    assert client.interactions.calls[0]["input"] == "Lane: analysis\n\ncustom prompt"


def test_records_written_counts_only_new_records(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "l.db"
    SqliteLedger(ledger_path).add(Hypothesis("org/fork", "old", "dedupe-jobs", 10.0))

    code, summary = _run(_argv("analysis", ledger_path), _calling("record_hypothesis", _hypothesis_args()), capsys)

    assert code == 0
    assert summary["records_written"] == 1


def test_optimizer_default_prompt_names_latest_hypothesis(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "l.db"
    ledger = SqliteLedger(ledger_path)
    ledger.add(Hypothesis("org/fork", "older", "dedupe-jobs", 10.0))
    hypothesis = Hypothesis("org/fork", "newer", "dep-cache", 60.0)
    ledger.add(hypothesis)
    ledger.add(Hypothesis("other/repo", "elsewhere", "dep-cache", 60.0))
    client = _calling(
        "report_candidate",
        {"hypothesis_id": hypothesis.id, "branch": "pc/cache", "summary": "cache deps"},
    )

    code, summary = _run(_argv("optimizer", ledger_path), client, capsys)

    assert code == 0
    assert summary["expected_record"] == "Candidate"
    assert summary["records_written"] == 1
    assert f"Hypothesis id: {hypothesis.id}\n" in str(client.interactions.calls[0]["input"])


def test_proof_default_prompt_inlines_latest_candidate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "l.db"
    ledger = SqliteLedger(ledger_path)
    candidate = Candidate(
        "h1", "pc/h1-dep-cache", "cache deps", repo="org/fork", files={".github/workflows/ci.yml": "on: push\n"}
    )
    ledger.add(candidate)
    client = _calling("plan_replays", {"candidate_id": candidate.id, "bug_shas": ["abc123"]})

    code, summary = _run(_argv("proof", ledger_path), client, capsys)

    assert code == 0
    assert summary["expected_record"] == "ReplayPlan"
    assert summary["records_written"] == 1
    assert len(ledger.all(ReplayPlan)) == 1
    prompt = str(client.interactions.calls[0]["input"])
    assert f"Candidate id: {candidate.id}\n" in prompt
    assert "cache deps" in prompt
    assert ".github/workflows/ci.yml" in prompt


@pytest.mark.parametrize(("lane", "kind"), [("optimizer", "Hypothesis"), ("proof", "Candidate")])
def test_missing_prerequisite_fails_before_calling_the_api(
    lane: str, kind: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _FakeClient()

    code, summary = _run(_argv(lane, tmp_path / "l.db"), client, capsys)

    assert code == 1
    assert summary["interaction_id"] is None
    assert summary["status"] == "error"
    assert summary["records_written"] == 0
    assert f"needs a {kind}" in summary["error"]
    assert client.interactions.calls == []


def test_task_override_skips_prerequisite_lookup(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = _FakeClient(_FakeInteraction(id="int-1", status="completed"))

    code, summary = _run(_argv("optimizer", tmp_path / "l.db", "--task", "hypothesis id: x"), client, capsys)

    assert code == 1  # completed, but no Candidate was written
    assert summary["status"] == "completed"
    assert summary["records_written"] == 0
    assert len(client.interactions.calls) == 1


def test_completed_lane_without_expected_record_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _FakeClient(_FakeInteraction(id="int-1", status="completed"))

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), client, capsys)

    assert code == 1
    assert summary["interaction_id"] == "int-1"
    assert summary["records_written"] == 0


def test_failed_lane_exits_nonzero_even_with_a_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = _calling("record_hypothesis", _hypothesis_args(), final_status="failed")

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), client, capsys)

    assert code == 1
    assert summary["status"] == "error"
    assert summary["error"] == "InteractionFailedError: lane 'analysis' interaction 'int-2' ended with status 'failed'"
    assert summary["records_written"] == 1


def test_lane_exception_is_reported_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(managed_agent, "MAX_TOOL_TURNS", 0)

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), _FakeClient(_FakeInteraction("i", "x")), capsys)

    assert code == 1
    assert summary["interaction_id"] is None
    assert summary["error"].startswith("MaxTurnsExceededError")


def test_missing_api_key_fails_without_network(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    code = cli.main(_argv("analysis", tmp_path / "l.db"))
    summary = json.loads(capsys.readouterr().out)

    assert code == 1
    assert summary["error"] == "KeyError: 'GEMINI_API_KEY'"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["smoke-lane", "--repo", "o/r", "--ledger", "l.db"],
        ["smoke-lane", "--lane", "rater", "--repo", "o/r", "--ledger", "l.db"],
        ["smoke-lane", "--lane", "analysis", "--ledger", "l.db"],
        ["smoke-lane", "--lane", "analysis", "--repo", "o/r"],
    ],
)
def test_invalid_arguments_exit_with_usage_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    assert "usage:" in capsys.readouterr().err


def _profile_then_record_client() -> _FakeClient:
    profile_call = _FakeStep(
        type="function_call",
        id="call-1",
        name="profile_ci",
        arguments={"repo": "org/fork", "workflow": "test.yml"},
    )
    record_call = _FakeStep(type="function_call", id="call-2", name="record_hypothesis", arguments=_hypothesis_args())
    return _FakeClient(
        _FakeInteraction(id="int-1", status="requires_action", steps=[profile_call]),
        _FakeInteraction(id="int-2", status="requires_action", steps=[record_call]),
        _FakeInteraction(id="int-3", status="completed"),
    )


def _fork_checkout(root: Path) -> Path:
    workflows = root / "fork" / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "test.yml").write_text((FIXTURES / "fastapi" / "test.yml").read_text())
    return root / "fork"


@pytest.fixture
def fastapi_history(monkeypatch: pytest.MonkeyPatch) -> None:
    with gzip.open(FIXTURES / "fastapi" / "runs.json.gz") as f:
        raw = json.load(f)
    monkeypatch.setattr(history, "fetch_history", lambda *_args, **_kwargs: raw)


@pytest.mark.usefixtures("fastapi_history")
def test_workspace_flag_lets_analysis_profile_ci_read_a_local_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "elsewhere"))
    checkout = _fork_checkout(tmp_path)
    client = _profile_then_record_client()

    code, summary = _run(_argv("analysis", tmp_path / "l.db", "--workspace", str(checkout)), client, capsys)

    assert (code, summary["status"], summary["records_written"]) == (0, "completed", 1)
    profile_result = client.interactions.calls[1]["input"][0]  # type: ignore[index]
    assert profile_result["name"] == "profile_ci"
    assert "is_error" not in profile_result
    assert profile_result["result"]["critical_path"]["jobs"][0] == "changes"
    assert os.environ["PIT_CREW_WORKSPACE"] == str(tmp_path / "elsewhere")  # never overwritten
    assert host_workspace.configured_workspace() == tmp_path / "elsewhere"  # the flag's binding was reset


@pytest.mark.usefixtures("fastapi_history")
def test_analysis_honors_pit_crew_workspace_env(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(_fork_checkout(tmp_path)))

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), _profile_then_record_client(), capsys)

    assert (code, summary["status"], summary["records_written"]) == (0, "completed", 1)


def test_workspace_flag_binds_the_context_without_touching_the_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path / "from-env"))
    environ_before = dict(os.environ)
    seen: list[tuple[Path, dict[str, str]]] = []

    def fake_run_lane(*_args: object, **_kwargs: object) -> _FakeInteraction:
        seen.append((analysis_tools.workspace(), dict(os.environ)))
        return _FakeInteraction(id="int-1", status="completed")

    monkeypatch.setattr(managed_agent, "run_lane", fake_run_lane)

    _run(_argv("analysis", tmp_path / "l.db", "--workspace", str(tmp_path)), _FakeClient(), capsys)

    assert seen == [(tmp_path, environ_before)]
    assert dict(os.environ) == environ_before
    assert host_workspace.configured_workspace() == tmp_path / "from-env"


def _tarball(files: dict[str, str]) -> bytes:
    """A gzipped tarball shaped like GitHub's: every member under one ``owner-repo-sha/`` folder."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        top = tarfile.TarInfo("org-fork-abc123")
        top.type = tarfile.DIRTYPE
        tar.addfile(top)
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"org-fork-abc123/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class _FakeDownload:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []
        self.tokens: list[str | None] = []

    def factory(self, token: str | None) -> host_workspace.Download:
        self.tokens.append(token)

        def download(url: str, dest: Path) -> None:
            self.urls.append(url)
            dest.write_bytes(self.body)

        return download


@pytest.mark.usefixtures("fastapi_history")
def test_analysis_downloads_the_repo_tarball_when_no_workspace_is_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIT_CREW_WORKSPACE", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    workflow = (FIXTURES / "fastapi" / "test.yml").read_text()
    fake = _FakeDownload(_tarball({".github/workflows/test.yml": workflow}))
    monkeypatch.setattr(host_workspace, "http_download", fake.factory)
    seen: list[Path] = []
    real_run_lane = managed_agent.run_lane

    def spying_run_lane(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401  pass-through spy
        seen.append(analysis_tools.workspace())
        return real_run_lane(*args, **kwargs)

    monkeypatch.setattr(managed_agent, "run_lane", spying_run_lane)
    client = _profile_then_record_client()

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), client, capsys)

    assert (code, summary["status"], summary["records_written"]) == (0, "completed", 1)
    assert fake.urls == ["https://api.github.com/repos/org/fork/tarball"]
    assert fake.tokens == ["tok"]
    profile_result = client.interactions.calls[1]["input"][0]  # type: ignore[index]
    assert "is_error" not in profile_result
    assert profile_result["result"]["critical_path"]["jobs"][0] == "changes"
    assert len(seen) == 1
    assert seen[0].name == "repo"
    assert seen[0].parent.name.startswith("pit-crew-workspace-")
    assert not seen[0].exists()  # deleted after the run
    assert "PIT_CREW_WORKSPACE" not in os.environ
    assert host_workspace.configured_workspace() is None


def test_tarball_workspace_is_deleted_when_the_lane_raises(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIT_CREW_WORKSPACE", raising=False)
    fake = _FakeDownload(_tarball({"README.md": "hi"}))
    monkeypatch.setattr(host_workspace, "http_download", fake.factory)
    seen: list[Path] = []

    def failing_run_lane(*_args: object, **_kwargs: object) -> _FakeInteraction:
        seen.append(analysis_tools.workspace())
        assert (seen[0] / "README.md").read_text() == "hi"
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(managed_agent, "run_lane", failing_run_lane)

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), _FakeClient(), capsys)

    assert code == 1
    assert summary["error"] == "RuntimeError: boom"
    assert len(seen) == 1
    assert not seen[0].parent.exists()


def test_tarball_download_failure_is_reported_before_calling_the_api(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIT_CREW_WORKSPACE", raising=False)

    def failing_factory(_token: str | None) -> host_workspace.Download:
        def download(_url: str, _dest: Path) -> None:
            msg = "offline"
            raise OSError(msg)

        return download

    monkeypatch.setattr(host_workspace, "http_download", failing_factory)
    client = _FakeClient()

    code, summary = _run(_argv("analysis", tmp_path / "l.db"), client, capsys)

    assert code == 1
    assert summary["error"] == "OSError: offline"
    assert client.interactions.calls == []


@pytest.mark.parametrize("lane", ["optimizer", "proof"])
def test_lanes_that_do_not_read_the_checkout_never_download_it(
    lane: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PIT_CREW_WORKSPACE", raising=False)
    monkeypatch.setattr(host_workspace, "http_download", lambda _token: pytest.fail("must not download"))
    monkeypatch.setattr(managed_agent, "run_lane", lambda *_a, **_k: _FakeInteraction(id="int-1", status="completed"))

    code, summary = _run(_argv(lane, tmp_path / "l.db", "--task", "t"), _FakeClient(), capsys)

    assert summary["status"] == "completed"
    assert code == 1  # the fake lane wrote no record


@pytest.mark.parametrize("use_flag", [True, False])
def test_analysis_with_missing_workspace_fails_before_calling_the_api(
    use_flag: bool,  # noqa: FBT001
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "no-such-checkout"
    monkeypatch.setenv("PIT_CREW_WORKSPACE", str(tmp_path if use_flag else missing))
    extra = ["--workspace", str(missing)] if use_flag else []
    client = _FakeClient()

    code, summary = _run(_argv("analysis", tmp_path / "l.db", *extra), client, capsys)

    assert code == 1
    assert summary["error"].startswith("MissingWorkspaceError")
    assert "--workspace" in summary["error"]
    assert client.interactions.calls == []


def test_proof_matches_candidate_repo_case_insensitively_and_picks_newest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger_path = tmp_path / "l.db"
    ledger = SqliteLedger(ledger_path)
    older = Candidate("h1", "pc/older", "older change", repo="Org/Fork")
    newest = Candidate("h2", "pc/newest", "newest change", repo="ORG/fork")
    ledger.add(older)
    ledger.add(Candidate("h3", "pc/other", "other repo", repo="org/forked"))
    ledger.add(newest)
    ledger.add(Candidate("h4", "pc/elsewhere", "elsewhere", repo="Other/Repo"))
    client = _calling("plan_replays", {"candidate_id": newest.id, "bug_shas": ["abc123"]})

    argv = ["smoke-lane", "--lane", "proof", "--repo", "org/FORK", "--ledger", str(ledger_path)]
    code, summary = _run(argv, client, capsys)

    assert code == 0
    assert summary["records_written"] == 1
    prompt = str(client.interactions.calls[0]["input"])
    assert f"Candidate id: {newest.id}\n" in prompt
    assert older.id not in prompt
    assert "pc/other" not in prompt
    assert "pc/elsewhere" not in prompt


def test_proof_ignores_candidates_for_other_repos(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger_path = tmp_path / "l.db"
    SqliteLedger(ledger_path).add(Candidate("h1", "pc/x", "x", repo="Other/Repo"))
    client = _FakeClient()

    code, summary = _run(_argv("proof", ledger_path), client, capsys)

    assert code == 1
    assert "needs a Candidate" in summary["error"]
    assert client.interactions.calls == []


@pytest.mark.parametrize("make_ledger", ["directory", "garbage"])
def test_unusable_ledger_is_reported_as_one_json_line(
    make_ledger: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if make_ledger == "directory":
        ledger_path = tmp_path / "ledger-dir"
        ledger_path.mkdir()
    else:
        ledger_path = tmp_path / "garbage.db"
        ledger_path.write_bytes(b"this is not a sqlite database" * 100)
    client = _FakeClient()

    code, summary = _run(_argv("optimizer", ledger_path), client, capsys)

    assert code == 1
    assert summary["status"] == "error"
    assert summary["interaction_id"] is None
    assert summary["records_written"] == 0
    assert summary["error"]
    assert client.interactions.calls == []


def test_parser_accepts_every_lane_and_optional_task() -> None:
    parser = cli._parser()  # noqa: SLF001
    for lane in cli.LANES:
        args = parser.parse_args(["smoke-lane", "--lane", lane, "--repo", "o/r", "--ledger", "l.db"])
        assert (args.lane, args.repo, args.ledger, args.task, args.workspace) == (lane, "o/r", "l.db", None, None)
    args = parser.parse_args(
        ["smoke-lane", "--lane", "proof", "--repo", "o/r", "--ledger", "l.db", "--task", "t", "--workspace", "w"]
    )
    assert (args.task, args.workspace) == ("t", "w")
