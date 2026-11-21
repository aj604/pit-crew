import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from pit_crew import cli, managed_agent
from pit_crew.contracts import Candidate, Hypothesis, ReplayPlan
from pit_crew.ledger import SqliteLedger


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
    assert managed_agent.repository_source("org/fork") in sources


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
    assert summary["status"] == "failed"
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


def test_parser_accepts_every_lane_and_optional_task() -> None:
    parser = cli._parser()  # noqa: SLF001
    for lane in cli.LANES:
        args = parser.parse_args(["smoke-lane", "--lane", lane, "--repo", "o/r", "--ledger", "l.db"])
        assert (args.lane, args.repo, args.ledger, args.task) == (lane, "o/r", "l.db", None)
    args = parser.parse_args(["smoke-lane", "--lane", "proof", "--repo", "o/r", "--ledger", "l.db", "--task", "t"])
    assert args.task == "t"
