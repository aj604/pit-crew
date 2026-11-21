"""Stepped and automatic playback engine for M1 Pit Wall demo scenarios."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from pit_crew import rater
from pit_crew.contracts import Candidate, Evidence, Hypothesis, Record, ReplayPlan, Verdict
from pit_crew.demo.scenarios import DemoScenario, get_scenario
from pit_crew.ledger import InMemoryLedger, Ledger


class InvalidDemoModeError(ValueError):
    """Raised when a demo playback mode is not 'auto' or 'step'."""

    def __init__(self, mode: str) -> None:
        """Build error for an invalid playback mode."""
        super().__init__(f"mode must be 'auto' or 'step', got {mode!r}")


class DemoEngine:
    """Drives M1 demo scenarios through a Ledger and broadcasts SSE events."""

    def __init__(self, ledger: Ledger | None = None, *, default_delay_seconds: float = 0.4) -> None:
        """Initialize the playback engine with a ledger and default auto-play delay."""
        self.ledger: Ledger = ledger if ledger is not None else InMemoryLedger()
        self.default_delay_seconds = default_delay_seconds
        self.active_scenario: DemoScenario | None = None
        self.mode: str = "idle"  # "idle" | "auto" | "step" | "completed"
        self.stage: str = "idle"
        self._queue: list[tuple[dict[str, Any], Record | None]] = []
        self._cursor: int = 0
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._auto_task: asyncio.Task[None] | None = None

    def _clear_ledger(self) -> None:
        if isinstance(self.ledger, InMemoryLedger):
            self.ledger._records.clear()  # noqa: SLF001

    def _build_steps(self, scenario: DemoScenario) -> list[tuple[dict[str, Any], Record | None]]:
        steps: list[tuple[dict[str, Any], Record | None]] = [
            (
                {
                    "type": "stage_changed",
                    "stage": "analysis",
                    "status": "running",
                    "message": f"Scanning workflow run history for {scenario.repo}...",
                },
                None,
            ),
            (
                {
                    "type": "record_added",
                    "kind": "Hypothesis",
                    "record": asdict(scenario.hypothesis),
                },
                scenario.hypothesis,
            ),
            (
                {
                    "type": "stage_changed",
                    "stage": "analysis",
                    "status": "completed",
                    "message": (
                        f"Proposed {scenario.hypothesis.skill} (~{scenario.hypothesis.expected_savings_seconds:.0f}s)"
                    ),
                },
                None,
            ),
            (
                {
                    "type": "stage_changed",
                    "stage": "optimizer",
                    "status": "running",
                    "message": f"Applying {scenario.hypothesis.skill} playbook on fork...",
                },
                None,
            ),
            (
                {
                    "type": "record_added",
                    "kind": "Candidate",
                    "record": asdict(scenario.candidate),
                },
                scenario.candidate,
            ),
            (
                {
                    "type": "stage_changed",
                    "stage": "optimizer",
                    "status": "completed",
                    "message": f"Pushed branch {scenario.candidate.branch}",
                },
                None,
            ),
            (
                {
                    "type": "stage_changed",
                    "stage": "proof",
                    "status": "running",
                    "message": "Mining historical red commits for touched checks...",
                },
                None,
            ),
            (
                {
                    "type": "record_added",
                    "kind": "ReplayPlan",
                    "record": asdict(scenario.replay_plan),
                },
                scenario.replay_plan,
            ),
            (
                {
                    "type": "stage_changed",
                    "stage": "proof",
                    "status": "completed",
                    "message": f"Selected {len(scenario.replay_plan.cases)} historical bug replays",
                },
                None,
            ),
            (
                {
                    "type": "stage_changed",
                    "stage": "ci",
                    "status": "running",
                    "message": "Running interleaved timing samples and bug replays...",
                },
                None,
            ),
        ]

        # Interleave baseline and candidate timing samples
        base = scenario.evidence.baseline_seconds
        cand = scenario.evidence.candidate_seconds
        for idx in range(max(len(base), len(cand))):
            if idx < len(base):
                steps.append(
                    (
                        {
                            "type": "timing_sample",
                            "candidate_id": scenario.candidate.id,
                            "side": "baseline",
                            "run_index": idx + 1,
                            "seconds": base[idx],
                        },
                        None,
                    )
                )
            if idx < len(cand):
                steps.append(
                    (
                        {
                            "type": "timing_sample",
                            "candidate_id": scenario.candidate.id,
                            "side": "candidate",
                            "run_index": idx + 1,
                            "seconds": cand[idx],
                        },
                        None,
                    )
                )

        # Bug replay outcomes
        steps.extend(
            (
                {
                    "type": "bug_replay_result",
                    "candidate_id": scenario.candidate.id,
                    **outcome,
                },
                None,
            )
            for outcome in scenario.bug_summaries.values()
        )

        steps.extend(
            [
                (
                    {
                        "type": "record_added",
                        "kind": "Evidence",
                        "record": asdict(scenario.evidence),
                    },
                    scenario.evidence,
                ),
                (
                    {
                        "type": "stage_changed",
                        "stage": "ci",
                        "status": "completed",
                        "message": (
                            f"Collected {len(base)}+{len(cand)} timing runs and {scenario.evidence.bugs_total} replays"
                        ),
                    },
                    None,
                ),
                (
                    {
                        "type": "stage_changed",
                        "stage": "rater",
                        "status": "running",
                        "message": "Running deterministic permutation test and bug gate...",
                    },
                    None,
                ),
            ]
        )

        # Always compute the real verdict with pit_crew.rater.rate()
        verdict = rater.rate(scenario.evidence)
        steps.extend(
            [
                (
                    {
                        "type": "record_added",
                        "kind": "Verdict",
                        "record": asdict(verdict),
                    },
                    verdict,
                ),
                (
                    {
                        "type": "stage_changed",
                        "stage": "rater",
                        "status": "completed",
                        "message": f"{'KEEP' if verdict.keep else 'REJECT'}: {verdict.reason}",
                    },
                    None,
                ),
            ]
        )
        return steps

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def _cancel_auto_task(self) -> None:
        if self._auto_task is not None and not self._auto_task.done():
            self._auto_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._auto_task
        self._auto_task = None

    async def start(
        self,
        scenario_id: str,
        *,
        mode: str = "auto",
        delay_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Reset ledger and start playing back ``scenario_id`` in ``auto`` or ``step`` mode."""
        if mode not in ("auto", "step"):
            raise InvalidDemoModeError(mode)
        scenario = get_scenario(scenario_id)
        await self._cancel_auto_task()
        self._clear_ledger()
        self.active_scenario = scenario
        self.mode = mode
        self.stage = "ready"
        self._queue = self._build_steps(scenario)
        self._cursor = 0

        if mode == "auto":
            delay = self.default_delay_seconds if delay_seconds is None else delay_seconds
            self._auto_task = asyncio.create_task(self._run_auto(delay))

        return {
            "status": "started",
            "scenario_id": scenario.id,
            "mode": mode,
            "total_events": len(self._queue),
        }

    async def _run_auto(self, delay_seconds: float) -> None:
        while self._cursor < len(self._queue):
            self.step()
            if delay_seconds > 0 and self._cursor < len(self._queue):
                await asyncio.sleep(delay_seconds)

    def step(self) -> dict[str, Any] | None:
        """Advance playback by one event, writing any associated record to the ledger."""
        if self._cursor >= len(self._queue):
            return None
        event, record = self._queue[self._cursor]
        self._cursor += 1
        if record is not None:
            self.ledger.add(record)
        if event["type"] == "stage_changed":
            self.stage = str(event["stage"])
        if self._cursor >= len(self._queue):
            self.mode = "completed"
        self._broadcast(event)
        return event

    async def reset(self) -> dict[str, str]:
        """Stop playback and reset ledger state to idle."""
        await self._cancel_auto_task()
        self._clear_ledger()
        self.active_scenario = None
        self.mode = "idle"
        self.stage = "idle"
        self._queue = []
        self._cursor = 0
        self._broadcast({"type": "stage_changed", "stage": "idle", "status": "reset", "message": "Demo reset"})
        return {"status": "reset"}

    def snapshot(self) -> dict[str, Any]:
        """Return all current ledger records plus active scenario display metadata."""
        metadata: dict[str, Any] = {}
        if self.active_scenario is not None:
            metadata = {
                "scenario_id": self.active_scenario.id,
                "title": self.active_scenario.title,
                "diff": self.active_scenario.diff,
                "bug_summaries": self.active_scenario.bug_summaries,
            }
        return {
            "mode": self.mode,
            "stage": self.stage,
            "hypotheses": [asdict(r) for r in self.ledger.all(Hypothesis)],
            "candidates": [asdict(r) for r in self.ledger.all(Candidate)],
            "replay_plans": [asdict(r) for r in self.ledger.all(ReplayPlan)],
            "evidence": [asdict(r) for r in self.ledger.all(Evidence)],
            "verdicts": [asdict(r) for r in self.ledger.all(Verdict)],
            "metadata": metadata,
        }

    async def wait_for_completion(self) -> None:
        """Await the active auto-playback task if one is running."""
        if self._auto_task is not None:
            await self._auto_task

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield live demo events for an SSE subscriber until disconnected."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._subscribers.discard(queue)
