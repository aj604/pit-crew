from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import pytest

from pit_crew import managed_agent, tools
from pit_crew.contracts import Candidate, Hypothesis, ReplayPlan
from pit_crew.dispatcher import next_lane
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import (
    BASE_AGENT,
    MODELS,
    InteractionFailedError,
    MaxTurnsExceededError,
    build_request,
    inline_bytes_source,
    normalize_repository_url,
    repository_source,
    run_lane,
)


def test_records_flow_from_hypothesis_to_replay_plan() -> None:
    build_request("analysis", "")  # each call registers that lane's tools
    build_request("optimizer", "")
    build_request("proof", "")
    ledger = InMemoryLedger()

    hypothesis_id = tools.call(
        "record_hypothesis",
        ledger,
        {"repo": "o/r", "description": "no cache", "skill": "dep-cache", "expected_savings_seconds": 120},
    )
    assert next_lane(ledger.get(Hypothesis, hypothesis_id)) == "optimizer"

    candidate_id = tools.call(
        "report_candidate", ledger, {"hypothesis_id": hypothesis_id, "branch": "pc/cache", "summary": "cache deps"}
    )
    assert next_lane(ledger.get(Candidate, candidate_id)) == "proof"

    plan_id = tools.call("plan_replays", ledger, {"candidate_id": candidate_id, "bug_shas": ["abc123"]})
    plan = ledger.get(ReplayPlan, plan_id)
    assert plan.bug_shas == ("abc123",)
    assert len(plan.cases) == 1
    assert plan.cases[0].commit_sha == "abc123"
    assert next_lane(plan) is None


def test_candidate_for_unknown_hypothesis_is_refused() -> None:
    build_request("optimizer", "")
    with pytest.raises(KeyError):
        tools.call("report_candidate", InMemoryLedger(), {"hypothesis_id": "nope", "branch": "b", "summary": "s"})


@pytest.mark.parametrize("lane", ["analysis", "optimizer", "proof"])
def test_request_carries_only_the_lanes_own_skill_and_tools(lane: str) -> None:
    for other in ("analysis", "optimizer", "proof"):
        build_request(other, "register all lane tools first")

    request = build_request(lane, "do the thing")
    targets = [source["target"] for source in request["environment"]["sources"]]
    assert targets[:2] == [".agents/AGENTS.md", f".agents/skills/{lane}/SKILL.md"]
    assert all(t == ".agents/AGENTS.md" or t.startswith(f".agents/skills/{lane}/") for t in targets)
    assert request["agent_config"]["model"] == MODELS[lane]
    assert MODELS[lane].startswith("gemini-4-")
    assert request["tools"]
    assert {t["name"] for t in request["tools"]} == {t.name for t in tools.for_lane(lane)}
    assert request["input"].startswith(f"Lane: {lane}")


def test_build_request_mounts_repo_playbooks_and_multimodal_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lanes_dir = tmp_path / "lanes"
    proof_dir = lanes_dir / "proof"
    playbooks_dir = proof_dir / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (proof_dir / "SKILL.md").write_text("# Proof\n")
    (playbooks_dir / "visual-cache.md").write_text("# Visual Cache\n")
    monkeypatch.setattr(managed_agent, "_LANES", lanes_dir)

    png = b"\x89PNG\r\n\x1a\nfake"
    request = build_request(
        "proof",
        "inspect failure screenshot",
        repo="https://github.com/org/fork",
        extra_sources=[inline_bytes_source("/workspace/.pit-crew/artifacts/fail.png", png)],
    )
    sources = request["environment"]["sources"]
    targets = [s["target"] for s in sources]
    assert ".agents/skills/proof/playbooks/visual-cache.md" in targets
    assert {"type": "repository", "source": "https://github.com/org/fork", "target": "/workspace"} in sources
    artifact = next(s for s in sources if s["target"].endswith("fail.png"))
    assert artifact["encoding"] == "base64"


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
    steps: list[_FakeStep] | None
    continuation_token: str | None = None
    output_text: str = ""
    environment_id: str | None = None


class _FakeInteractionsAPI:
    def __init__(self, responses: list[_FakeInteraction], polled: list[_FakeInteraction] | None = None) -> None:
        self._responses = list(responses)
        self._polled = list(polled or [])
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _FakeInteraction:
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def get(self, interaction_id: str, /) -> _FakeInteraction:
        assert interaction_id
        return self._polled.pop(0)


class _FakeClient:
    def __init__(self, responses: list[_FakeInteraction], polled: list[_FakeInteraction] | None = None) -> None:
        self.interactions = _FakeInteractionsAPI(responses, polled)


def test_run_lane_executes_polling_continuation_and_tool_loop_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_agent, "POLL_INTERVAL_SECONDS", 0.0)
    ledger = InMemoryLedger()
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="in_progress", steps=[]),
            _FakeInteraction(
                id="int-2",
                status="requires_action",
                steps=[
                    _FakeStep(type="user_input"),
                    _FakeStep(
                        type="function_call",
                        id="call-disallowed",
                        name="plan_replays",
                        arguments={"candidate_id": "x", "bug_shas": ["abc"]},
                    ),
                    _FakeStep(
                        type="function_call",
                        id="call-bad-args",
                        name="record_hypothesis",
                        arguments={"repo": "org/fork"},
                    ),
                    _FakeStep(
                        type="function_call",
                        id="call-1",
                        name="record_hypothesis",
                        arguments={
                            "repo": "org/fork",
                            "description": "uv cache disabled",
                            "skill": "dep-cache",
                            "expected_savings_seconds": 95.0,
                        },
                    ),
                ],
            ),
            _FakeInteraction(id="int-3", status="completed", steps=[], output_text="Recorded 1 hypothesis."),
        ],
        polled=[
            _FakeInteraction(
                id="int-1",
                status="requires_continuation",
                steps=[],
                continuation_token="tok-1",  # noqa: S106
            )
        ],
    )

    final = run_lane("analysis", "analyze org/fork", ledger=ledger, client=fake_client)
    assert final.status == "completed"
    recorded = ledger.all(Hypothesis)
    assert len(recorded) == 1
    assert recorded[0].skill == "dep-cache"

    tool_reply = fake_client.interactions.calls[2]["input"]
    assert isinstance(tool_reply, list)
    assert tool_reply[0]["is_error"] is True
    assert tool_reply[1]["is_error"] is True
    assert tool_reply[2]["result"] == recorded[0].id


def test_run_lane_raises_when_exceeding_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_agent, "MAX_TOOL_TURNS", 1)
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(
                id="int-1",
                status="requires_action",
                steps=[
                    _FakeStep(
                        type="function_call",
                        id="call-1",
                        name="record_hypothesis",
                        arguments={
                            "repo": "org/fork",
                            "description": "no cache",
                            "skill": "dep-cache",
                            "expected_savings_seconds": 30.0,
                        },
                    )
                ],
            ),
            _FakeInteraction(id="int-2", status="requires_action", steps=[]),
        ]
    )
    with pytest.raises(MaxTurnsExceededError, match="exceeded 1"):
        run_lane("analysis", "loop forever", client=fake_client)


def test_run_lane_forwards_repo_to_interaction_request() -> None:
    fake_client = _FakeClient(responses=[_FakeInteraction(id="int-1", status="completed", steps=[])])

    final = run_lane("optimizer", "add dep cache", repo="https://github.com/org/fork", client=fake_client)
    assert final.status == "completed"
    assert len(fake_client.interactions.calls) == 1

    environment = fake_client.interactions.calls[0]["environment"]
    assert isinstance(environment, dict)
    sources = environment["sources"]
    assert isinstance(sources, list)
    assert repository_source("https://github.com/org/fork") in sources


def test_run_lane_omits_repository_source_when_repo_is_omitted() -> None:
    fake_client = _FakeClient(responses=[_FakeInteraction(id="int-1", status="completed", steps=[])])

    final = run_lane("optimizer", "add dep cache", client=fake_client)
    assert final.status == "completed"
    assert len(fake_client.interactions.calls) == 1

    environment = fake_client.interactions.calls[0]["environment"]
    assert isinstance(environment, dict)
    sources = environment["sources"]
    assert isinstance(sources, list)
    assert not any(source.get("type") == "repository" for source in sources)


def test_run_lane_forwards_repo_to_the_request() -> None:
    fake_client = _FakeClient(responses=[_FakeInteraction(id="int-1", status="completed", steps=[])])
    run_lane("analysis", "analyze", repo="org/fork", client=fake_client)
    sources = fake_client.interactions.calls[0]["environment"]["sources"]  # type: ignore[index]
    assert {"type": "repository", "source": "https://github.com/org/fork", "target": "/workspace"} in sources


def _record_call(call_id: str = "call-1") -> _FakeStep:
    return _FakeStep(
        type="function_call",
        id=call_id,
        name="record_hypothesis",
        arguments={"repo": "org/fork", "description": "no cache", "skill": "dep-cache", "expected_savings_seconds": 1},
    )


def test_follow_up_turns_preserve_environment_agent_config_and_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_agent, "POLL_INTERVAL_SECONDS", 0.0)
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="in_progress", steps=[]),
            _FakeInteraction(id="int-2", status="requires_action", steps=[_record_call()], environment_id="env-2"),
            _FakeInteraction(id="int-3", status="completed", steps=[]),
        ],
        polled=[
            _FakeInteraction(
                id="int-1",
                status="requires_continuation",
                steps=[],
                continuation_token="tok-1",  # noqa: S106
                environment_id="env-1",
            )
        ],
    )

    run_lane("analysis", "analyze", client=fake_client)

    initial, continuation, action = fake_client.interactions.calls
    for follow_up in (continuation, action):
        assert follow_up["agent"] == BASE_AGENT
        assert (
            follow_up["agent_config"] == initial["agent_config"] == {"type": "antigravity", "model": MODELS["analysis"]}
        )
        assert follow_up["tools"] == initial["tools"]
        assert follow_up["tools"]
    assert continuation["previous_interaction_id"] == "int-1"
    assert continuation["continuation_token"] == "tok-1"  # noqa: S105
    assert continuation["environment"] == "env-1"
    assert action["previous_interaction_id"] == "int-2"
    assert action["environment"] == "env-2"
    assert isinstance(action["input"], list)


def test_follow_up_falls_back_to_the_initial_environment_id() -> None:
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="requires_action", steps=[_record_call()], environment_id="env-1"),
            _FakeInteraction(id="int-2", status="requires_action", steps=[_record_call("call-2")]),
            _FakeInteraction(id="int-3", status="completed", steps=[]),
        ]
    )

    run_lane("analysis", "analyze", client=fake_client)

    assert [c["environment"] for c in fake_client.interactions.calls[1:]] == ["env-1", "env-1"]


def test_follow_up_remembers_an_environment_id_seen_only_while_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_agent, "POLL_INTERVAL_SECONDS", 0.0)
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="queued", steps=[]),
            _FakeInteraction(id="int-2", status="requires_action", steps=[_record_call()]),
            _FakeInteraction(id="int-3", status="completed", steps=[]),
        ],
        polled=[
            _FakeInteraction(id="int-1", status="in_progress", steps=[], environment_id="env-polled"),
            _FakeInteraction(id="int-1", status="requires_action", steps=[_record_call()]),
        ],
    )

    run_lane("analysis", "analyze", client=fake_client)

    assert [c["environment"] for c in fake_client.interactions.calls[1:]] == ["env-polled", "env-polled"]


def test_follow_up_remembers_an_environment_id_on_a_pending_create_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_agent, "POLL_INTERVAL_SECONDS", 0.0)
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="queued", steps=[], environment_id="env-created"),
            _FakeInteraction(id="int-2", status="completed", steps=[]),
        ],
        polled=[_FakeInteraction(id="int-1", status="requires_action", steps=[_record_call()])],
    )

    run_lane("analysis", "analyze", client=fake_client)

    assert fake_client.interactions.calls[1]["environment"] == "env-created"


def test_follow_up_remembers_an_environment_id_first_assigned_on_a_later_turn() -> None:
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="requires_action", steps=[_record_call()]),
            _FakeInteraction(
                id="int-2", status="requires_action", steps=[_record_call("call-2")], environment_id="env-2"
            ),
            _FakeInteraction(id="int-3", status="requires_action", steps=[_record_call("call-3")]),
            _FakeInteraction(id="int-4", status="completed", steps=[]),
        ]
    )

    run_lane("analysis", "analyze", client=fake_client)

    environments = [c["environment"] for c in fake_client.interactions.calls[1:]]
    assert isinstance(environments[0], dict)
    assert environments[1:] == ["env-2", "env-2"]


def test_follow_up_resends_environment_config_when_no_id_was_assigned() -> None:
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="requires_action", steps=[_record_call()]),
            _FakeInteraction(id="int-2", status="completed", steps=[]),
        ]
    )

    run_lane("analysis", "analyze", client=fake_client)

    initial, action = fake_client.interactions.calls
    assert action["environment"] == initial["environment"]
    assert isinstance(action["environment"], dict)


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired"])
def test_terminal_failure_returned_by_create_raises(status: str) -> None:
    fake_client = _FakeClient(responses=[_FakeInteraction(id="int-9", status=status, steps=[])])

    with pytest.raises(InteractionFailedError) as raised:
        run_lane("proof", "check", client=fake_client)

    assert raised.value.lane == "proof"
    assert raised.value.interaction_id == "int-9"
    assert raised.value.status == status
    assert str(raised.value) == f"lane 'proof' interaction 'int-9' ended with status {status!r}"


def test_terminal_failure_seen_while_polling_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_agent, "POLL_INTERVAL_SECONDS", 0.0)
    fake_client = _FakeClient(
        responses=[_FakeInteraction(id="int-1", status="queued", steps=[])],
        polled=[
            _FakeInteraction(id="int-1", status="in_progress", steps=[]),
            _FakeInteraction(id="int-1", status="expired", steps=[]),
        ],
    )

    with pytest.raises(InteractionFailedError, match="'analysis' interaction 'int-1' ended with status 'expired'"):
        run_lane("analysis", "analyze", client=fake_client)
    assert len(fake_client.interactions.calls) == 1


def test_non_failure_terminal_status_is_returned_not_raised() -> None:
    fake_client = _FakeClient(responses=[_FakeInteraction(id="int-1", status="incomplete", steps=[])])

    assert run_lane("analysis", "analyze", client=fake_client).status == "incomplete"


class _Opaque:
    @override
    def __str__(self) -> str:
        return "opaque"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("plain", "plain"),
        ({"k": [1, None]}, {"k": [1, None]}),
        ([1, "a"], '[1, "a"]'),
        ((1, 2), "[1, 2]"),
        (3, "3"),
        (2.5, "2.5"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
        ([_Opaque()], '["opaque"]'),
    ],
)
def test_tool_outputs_are_serialized_for_the_sdk(
    monkeypatch: pytest.MonkeyPatch, output: object, expected: object
) -> None:
    monkeypatch.setattr(tools, "call", lambda *_args, **_kwargs: output)
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="requires_action", steps=[_record_call()]),
            _FakeInteraction(id="int-2", status="completed", steps=[]),
        ]
    )

    run_lane("analysis", "analyze", client=fake_client)

    reply = fake_client.interactions.calls[1]["input"]
    assert isinstance(reply, list)
    assert reply[0]["result"] == expected
    assert "is_error" not in reply[0]


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("owner/name", "https://github.com/owner/name"),
        ("  owner/name/ ", "https://github.com/owner/name"),
        ("https://github.com/owner/name", "https://github.com/owner/name"),
        ("https://gitlab.example.com/group/name.git", "https://gitlab.example.com/group/name.git"),
    ],
)
def test_repository_identifiers_are_normalized_to_urls(repo: str, expected: str) -> None:
    assert normalize_repository_url(repo) == expected
    assert repository_source(repo, "/src") == {"type": "repository", "source": expected, "target": "/src"}
