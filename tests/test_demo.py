"""Tests for the M1 offline demo scenario engine and REST/SSE playback API."""

import asyncio
import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from pit_crew import rater
from pit_crew.demo import SCENARIOS, DemoEngine, create_demo_router, get_scenario
from pit_crew.ledger import InMemoryLedger
from pit_crew.server import create_app


def test_scenarios_produce_expected_real_verdicts() -> None:
    """All 3 M1 scenarios produce the expected Verdict via pit_crew.rater.rate()."""
    missed = rater.rate(SCENARIOS["rejected-missed-bug"].evidence)
    assert missed.keep is False
    assert missed.reason == "caught 3/4 bugs"

    kept = rater.rate(SCENARIOS["kept-dep-cache"].evidence)
    assert kept.keep is True
    assert kept.speedup > 1.8
    assert kept.p_value is not None
    assert kept.p_value <= rater.MAX_P_VALUE
    assert "caught 4/4 bugs" in kept.reason

    noise = rater.rate(SCENARIOS["rejected-noise"].evidence)
    assert noise.keep is False
    assert noise.reason == "speedup is within noise"
    assert noise.p_value is not None
    assert noise.p_value > rater.MAX_P_VALUE


def test_get_scenario_unknown_raises() -> None:
    with pytest.raises(KeyError, match="Unknown demo scenario"):
        get_scenario("does-not-exist")


def test_engine_step_mode_and_sse_broadcast() -> None:
    async def _run() -> None:
        ledger = InMemoryLedger()
        engine = DemoEngine(ledger=ledger, default_delay_seconds=0.0)

        received: list[dict[str, object]] = []

        async def _collect() -> None:
            async for event in engine.subscribe():
                received.append(event)
                if (
                    event.get("type") == "stage_changed"
                    and event.get("stage") == "rater"
                    and event.get("status") == "completed"
                ):
                    break

        sub_task = asyncio.create_task(_collect())
        await asyncio.sleep(0)  # allow subscriber to register

        start_info = await engine.start("kept-dep-cache", mode="step")
        assert start_info["status"] == "started"
        assert start_info["mode"] == "step"

        steps = 0
        while engine.step() is not None:
            steps += 1
        assert steps == start_info["total_events"]
        assert engine.mode == "completed"

        await asyncio.wait_for(sub_task, timeout=2.0)
        assert len(received) == steps

        snap = engine.snapshot()
        assert len(snap["hypotheses"]) == 1
        assert len(snap["candidates"]) == 1
        assert len(snap["replay_plans"]) == 1
        assert len(snap["evidence"]) == 1
        assert len(snap["verdicts"]) == 1
        assert snap["verdicts"][0]["keep"] is True
        assert snap["metadata"]["scenario_id"] == "kept-dep-cache"

        reset_info = await engine.reset()
        assert reset_info == {"status": "reset"}
        assert engine.snapshot()["verdicts"] == []

    asyncio.run(_run())


def test_engine_auto_mode_completes() -> None:
    async def _run() -> None:
        engine = DemoEngine(default_delay_seconds=0.0)
        await engine.start("rejected-missed-bug", mode="auto", delay_seconds=0.0)
        await asyncio.wait_for(engine.wait_for_completion(), timeout=2.0)
        snap = engine.snapshot()
        assert snap["mode"] == "completed"
        assert snap["verdicts"][0]["keep"] is False
        assert snap["verdicts"][0]["reason"] == "caught 3/4 bugs"

    asyncio.run(_run())


def test_demo_rest_endpoints() -> None:
    app = create_app(ledger=InMemoryLedger())
    client = TestClient(app)

    res = client.get("/api/scenarios")
    assert res.status_code == 200
    ids = {item["id"] for item in res.json()}
    assert ids == {"rejected-missed-bug", "kept-dep-cache", "rejected-noise"}

    bad = client.post("/api/demo/start", json={"scenario_id": "nope", "mode": "step"})
    assert bad.status_code == 404

    bad_mode = client.post("/api/demo/start", json={"scenario_id": "kept-dep-cache", "mode": "turbo"})
    assert bad_mode.status_code == 400

    started = client.post("/api/demo/start", json={"scenario_id": "rejected-noise", "mode": "step"})
    assert started.status_code == 200
    total = started.json()["total_events"]

    for _ in range(total):
        step_res = client.post("/api/demo/step")
        assert step_res.status_code == 200
        assert step_res.json()["status"] == "ok"

    done_res = client.post("/api/demo/step")
    assert done_res.status_code == 200
    assert done_res.json() == {"status": "completed", "event": None}

    ledger_res = client.get("/api/ledger")
    assert ledger_res.status_code == 200
    data = ledger_res.json()
    assert len(data["verdicts"]) == 1
    assert data["verdicts"][0]["keep"] is False
    assert data["verdicts"][0]["reason"] == "speedup is within noise"

    reset_res = client.post("/api/demo/reset")
    assert reset_res.status_code == 200
    assert client.get("/api/ledger").json()["verdicts"] == []


def test_sse_stream_endpoint_formats_events() -> None:
    async def _run() -> None:
        engine = DemoEngine(ledger=InMemoryLedger())
        router = create_demo_router(engine)
        events_route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == "/api/events")
        response = await events_route.endpoint()

        async def _read_one() -> str:
            async for chunk in response.body_iterator:
                return chunk.decode() if isinstance(chunk, bytes) else chunk
            return ""

        reader = asyncio.create_task(_read_one())
        await asyncio.sleep(0)
        await engine.start("kept-dep-cache", mode="step")
        engine.step()

        raw = await asyncio.wait_for(reader, timeout=2.0)
        assert raw.startswith("data: ")
        payload = json.loads(raw.removeprefix("data: ").strip())
        assert payload["type"] == "stage_changed"
        assert payload["stage"] == "analysis"

    asyncio.run(_run())
