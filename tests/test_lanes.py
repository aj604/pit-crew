import json
from pathlib import Path

import pytest

from pit_crew import tools
from pit_crew.contracts import Candidate, Hypothesis, ReplayPlan
from pit_crew.dispatcher import next_lane
from pit_crew.lanes.analysis.tools import slowest_jobs
from pit_crew.ledger import InMemoryLedger
from pit_crew.managed_agent import build_request

FIXTURES = Path(__file__).parent / "fixtures"


def test_analysis_ranks_jobs_by_total_time():
    job_runs = json.loads((FIXTURES / "job_runs.json").read_text())
    assert slowest_jobs(job_runs) == [("test", 1500.0), ("build", 500.0), ("lint", 60.0)]


def test_records_flow_from_hypothesis_to_replay_plan():
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
    assert next_lane(plan) is None


def test_candidate_for_unknown_hypothesis_is_refused():
    build_request("optimizer", "")
    with pytest.raises(KeyError):
        tools.call("report_candidate", InMemoryLedger(), {"hypothesis_id": "nope", "branch": "b", "summary": "s"})


@pytest.mark.parametrize("lane", ["analysis", "optimizer", "proof"])
def test_request_carries_only_the_lanes_own_skill_and_tools(lane):
    request = build_request(lane, "do the thing")
    targets = [source["target"] for source in request["environment"]["sources"]]
    assert targets == [".agents/AGENTS.md", f".agents/skills/{lane}/SKILL.md"]
    assert len(request["tools"]) == 1
    assert request["input"].startswith(f"Lane: {lane}")
