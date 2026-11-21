"""Thread-safe registry mapping GitHub Actions run IDs to completion events and results.

Waiters may block from an asyncio event loop (:meth:`RunTracker.wait`) or from a worker
thread (:meth:`RunTracker.wait_sync`). :meth:`RunTracker.resolve` may be called from any
thread and wakes both kinds of waiter immediately.
"""

import asyncio
import threading
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


_AsyncWaiter = tuple[asyncio.AbstractEventLoop, "asyncio.Future[WorkflowRunResult | None]"]


class _RunEntry:
    """Per-run state. Mutated only while holding the owning tracker's lock."""

    __slots__ = ("async_waiters", "done", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: WorkflowRunResult | None = None
        self.async_waiters: list[_AsyncWaiter] = []


def _settle(future: "asyncio.Future[WorkflowRunResult | None]", value: WorkflowRunResult | None) -> None:
    if not future.done():
        future.set_result(value)


def _wake_async(waiters: list[_AsyncWaiter], value: WorkflowRunResult | None) -> None:
    for loop, future in waiters:
        try:
            loop.call_soon_threadsafe(_settle, future, value)
        except RuntimeError:
            # The waiter's event loop has already closed; nobody is left to wake.
            continue


class RunTracker:
    """Thread-safe in-memory registry for awaiting GitHub Actions workflow run completions."""

    def __init__(self) -> None:
        """Initialize an empty run tracker."""
        self._lock = threading.Lock()
        self._entries: dict[int, _RunEntry] = {}

    def _entry(self, run_id: int) -> _RunEntry:
        """Return the entry for ``run_id``, creating it. Caller must hold ``self._lock``."""
        entry = self._entries.get(run_id)
        if entry is None:
            entry = _RunEntry()
            self._entries[run_id] = entry
        return entry

    def register(self, run_id: int) -> threading.Event:
        """Register a workflow run ID and return its thread-safe completion event.

        If the run ID is already registered or resolved, the existing event is returned.
        """
        with self._lock:
            return self._entry(run_id).done

    def resolve(self, result: WorkflowRunResult) -> bool:
        """Resolve a tracked workflow run with its result and wake all waiters.

        Safe to call from any thread. Safely ignores unknown or already-resolved run IDs
        without raising.

        Returns:
            True if the run was tracked and newly resolved, False otherwise.
        """
        with self._lock:
            entry = self._entries.get(result.run_id)
            if entry is None or entry.done.is_set():
                return False
            entry.result = result
            entry.done.set()
            waiters, entry.async_waiters = entry.async_waiters, []
        _wake_async(waiters, result)
        return True

    async def wait(self, run_id: int, timeout: float | None = None) -> WorkflowRunResult | None:  # noqa: ASYNC109
        """Wait on the running event loop for a workflow run to resolve.

        Automatically registers the run ID if it is not yet tracked.

        Returns:
            The resolved WorkflowRunResult, or None if the timeout elapsed first or the
            run was discarded while waiting.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[WorkflowRunResult | None] = loop.create_future()
        waiter: _AsyncWaiter = (loop, future)
        with self._lock:
            entry = self._entry(run_id)
            if entry.done.is_set():
                return entry.result
            entry.async_waiters.append(waiter)
        try:
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError:
            # A resolve may have landed between the timeout firing and now.
            with self._lock:
                return entry.result if entry.done.is_set() else None
        finally:
            with self._lock:
                if waiter in entry.async_waiters:
                    entry.async_waiters.remove(waiter)

    def wait_sync(self, run_id: int, timeout: float | None = None) -> WorkflowRunResult | None:
        """Block the calling thread until a workflow run resolves, with an optional timeout.

        Intended for worker threads (for example a synchronous REST poller); do not call it
        from an event loop thread, which it would block. Automatically registers the run ID
        if it is not yet tracked, and returns immediately if it is already resolved.

        Returns:
            The resolved WorkflowRunResult, or None if the timeout elapsed first or the
            run was discarded while waiting.
        """
        with self._lock:
            entry = self._entry(run_id)
        entry.done.wait(timeout)
        with self._lock:
            return entry.result

    def get(self, run_id: int) -> WorkflowRunResult | None:
        """Return the resolved result for a run ID, or None if not yet resolved."""
        with self._lock:
            entry = self._entries.get(run_id)
            return entry.result if entry is not None else None

    def is_tracked(self, run_id: int) -> bool:
        """Return whether a run ID is currently registered or resolved."""
        with self._lock:
            return run_id in self._entries

    def discard(self, run_id: int) -> None:
        """Remove a run ID from the tracker if present.

        Any waiters still blocked on the discarded run wake immediately and receive None.
        A later ``register()`` of the same run ID starts a fresh entry.
        """
        with self._lock:
            entry = self._entries.pop(run_id, None)
            if entry is None:
                return
            entry.done.set()
            waiters, entry.async_waiters = entry.async_waiters, []
        _wake_async(waiters, entry.result)
