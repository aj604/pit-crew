"""FastAPI router exposing M1 demo playback endpoints and SSE event stream."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from pit_crew.demo.engine import DemoEngine
from pit_crew.demo.scenarios import SCENARIOS


@dataclass(frozen=True)
class DemoStartRequest:
    """Request body for POST /api/demo/start."""

    scenario_id: str
    mode: str = "auto"
    delay_seconds: float | None = None


async def _event_stream(engine: DemoEngine) -> AsyncIterator[str]:
    async for event in engine.subscribe():
        yield f"data: {json.dumps(event)}\n\n"


def create_demo_router(
    engine: DemoEngine,
    *,
    dependencies: list[Any] | None = None,
) -> APIRouter:
    """Build the FastAPI router for Pit Wall M1 demo playback."""
    router = APIRouter(dependencies=dependencies or [])

    @router.get("/api/scenarios")
    def list_scenarios() -> list[dict[str, str]]:
        """List available M1 offline demo scenarios."""
        return [s.summary_dict() for s in SCENARIOS.values()]

    @router.get("/api/ledger")
    def get_ledger() -> dict[str, Any]:
        """Return all current ledger records and active scenario display metadata."""
        return engine.snapshot()

    @router.post("/api/demo/start")
    async def start_demo(payload: DemoStartRequest) -> dict[str, Any]:
        """Reset ledger and start playing back the requested scenario."""
        try:
            return await engine.start(
                payload.scenario_id,
                mode=payload.mode,
                delay_seconds=payload.delay_seconds,
            )
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @router.post("/api/demo/step")
    def step_demo() -> dict[str, Any]:
        """Advance playback by one event in step mode."""
        event = engine.step()
        return {"status": "completed" if event is None else "ok", "event": event}

    @router.post("/api/demo/reset")
    async def reset_demo() -> dict[str, str]:
        """Stop playback and reset ledger state to idle."""
        return await engine.reset()

    @router.get("/api/events")
    async def stream_events() -> StreamingResponse:
        """Stream ordered JSON demo events as Server-Sent Events."""
        return StreamingResponse(_event_stream(engine), media_type="text/event-stream")

    return router
