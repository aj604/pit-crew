"""Awaitable registry mapping GitHub Actions run IDs to completion events and results."""

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkflowRunResult:
    """Completion details for a GitHub Actions workflow run."""

    run_id: int
    status: str
    conclusion: str | None = None
    head_sha: str | None = None
    html_url: str | None = None
    duration_seconds: float | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


class RunTracker:
    """In-memory registry for awaiting GitHub Actions workflow run completions."""

    def __init__(self) -> None:
        """Initialize an empty run tracker."""
        self._events: dict[int, asyncio.Event] = {}
        self._results: dict[int, WorkflowRunResult] = {}

    def register(self, run_id: int) -> asyncio.Event:
        """Register a workflow run ID and return its completion event.

        If the run ID is already registered or resolved, the existing event is returned.
        """
        event = self._events.get(run_id)
        if event is None:
            event = asyncio.Event()
            self._events[run_id] = event
        return event

    def resolve(self, result: WorkflowRunResult) -> bool:
        """Resolve a tracked workflow run with its result and wake all waiters.

        Safely ignores unknown or already-resolved run IDs without raising.

        Returns:
            True if the run was tracked and newly resolved, False otherwise.
        """
        run_id = result.run_id
        event = self._events.get(run_id)
        if event is None or run_id in self._results:
            return False
        self._results[run_id] = result
        event.set()
        return True

    async def wait(self, run_id: int, timeout: float | None = None) -> WorkflowRunResult | None:  # noqa: ASYNC109
        """Wait for a workflow run to resolve, with an optional timeout.

        Automatically registers the run ID if it is not yet tracked.

        Returns:
            The resolved WorkflowRunResult, or None if the timeout elapsed first.
        """
        if run_id in self._results:
            return self._results[run_id]
        event = self.register(run_id)
        try:
            async with asyncio.timeout(timeout):
                await event.wait()
        except TimeoutError:
            return None
        return self._results[run_id]

    def get(self, run_id: int) -> WorkflowRunResult | None:
        """Return the resolved result for a run ID, or None if not yet resolved."""
        return self._results.get(run_id)

    def is_tracked(self, run_id: int) -> bool:
        """Return whether a run ID is currently registered or resolved."""
        return run_id in self._events or run_id in self._results

    def discard(self, run_id: int) -> None:
        """Remove a run ID's event and result from the tracker if present."""
        self._events.pop(run_id, None)
        self._results.pop(run_id, None)
