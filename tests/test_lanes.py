import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pit_crew import managed_agent, tools
from pit_crew.contracts import Candidate, Hypothesis, ReplayPlan
from pit_crew.dispatcher import next_lane
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import (
    MODELS,
    MaxTurnsExceededError,
    build_request,
    inline_bytes_source,
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
    environment_id: str | None = None
    output_text: str = ""


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
            _FakeInteraction(id="int-1", status="in_progress", steps=[], environment_id="env-1"),
            _FakeInteraction(
                id="int-2",
                status="requires_action",
                environment_id="env-1",
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
            _FakeInteraction(
                id="int-3", status="completed", steps=[], environment_id="env-1", output_text="Recorded 1 hypothesis."
            ),
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

    final = run_lane("analysis", "analyze org/fork", ledger=ledger, client=fake_client)
    assert final.status == "completed"
    recorded = ledger.all(Hypothesis)
    assert len(recorded) == 1
    assert recorded[0].skill == "dep-cache"

    continuation_call = fake_client.interactions.calls[1]
    assert continuation_call["agent_config"] == {"type": "antigravity", "model": MODELS["analysis"]}
    assert continuation_call["environment"] == {"type": "remote", "environment_id": "env-1"}
    assert continuation_call["tools"] == fake_client.interactions.calls[0]["tools"]

    tool_call = fake_client.interactions.calls[2]
    assert tool_call["agent_config"] == {"type": "antigravity", "model": MODELS["analysis"]}
    assert tool_call["environment"] == {"type": "remote", "environment_id": "env-1"}
    assert tool_call["tools"] == fake_client.interactions.calls[0]["tools"]
    tool_reply = tool_call["input"]
    assert isinstance(tool_reply, list)
    assert tool_reply[0]["is_error"] is True
    assert tool_reply[1]["is_error"] is True
    assert tool_reply[2]["result"] == recorded[0].id


def test_run_lane_forwards_repo_and_falls_back_when_environment_id_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "call", lambda _name, _ledger, _args: [{"path": "src/a.py", "line": 2}])
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(
                id="int-1",
                status="requires_action",
                steps=[
                    _FakeStep(
                        type="function_call",
                        id="call-list",
                        name="record_hypothesis",
                        arguments={},
                    )
                ],
            ),
            _FakeInteraction(id="int-2", status="completed", steps=[]),
        ]
    )

    final = run_lane("analysis", "inspect fork", repo="https://github.com/org/fork", client=fake_client)
    assert final.status == "completed"

    initial_env = fake_client.interactions.calls[0]["environment"]
    assert isinstance(initial_env, dict)
    assert {"type": "repository", "source": "https://github.com/org/fork", "target": "/workspace"} in initial_env[
        "sources"
    ]

    follow_up = fake_client.interactions.calls[1]
    assert follow_up["environment"] == initial_env
    tool_reply = follow_up["input"]
    assert isinstance(tool_reply, list)
    result = tool_reply[0]["result"]
    assert isinstance(result, str)
    assert json.loads(result) == [{"path": "src/a.py", "line": 2}]


def test_serialize_tool_result_accepts_all_output_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs: list[object] = [{"ok": True}, [1, 2], 42, True, None, object()]
    monkeypatch.setattr(tools, "call", lambda _name, _ledger, _args: outputs.pop(0))

    steps = [_FakeStep(type="function_call", id=f"c-{i}", name="record_hypothesis", arguments={}) for i in range(6)]
    fake_client = _FakeClient(
        responses=[
            _FakeInteraction(id="int-1", status="requires_action", steps=steps, environment_id="env-2"),
            _FakeInteraction(id="int-2", status="completed", steps=[], environment_id="env-2"),
        ]
    )

    run_lane("analysis", "test shapes", client=fake_client)
    tool_reply = fake_client.interactions.calls[1]["input"]
    assert isinstance(tool_reply, list)
    results = [item["result"] for item in tool_reply]
    assert results[0] == {"ok": True}
    assert results[1] == "[1, 2]"
    assert results[2] == "42"
    assert results[3] == "true"
    assert results[4] == "null"
    assert isinstance(results[5], str)


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
