import asyncio
import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pit_crew.run_tracker import RunTracker, WorkflowRunResult, _RunEntry


def _make_result(run_id: int, conclusion: str = "success") -> WorkflowRunResult:
    return WorkflowRunResult(
        run_id=run_id,
        status="completed",
        conclusion=conclusion,
        head_sha="abc123",
        html_url=f"https://github.com/aj604/pit-crew/actions/runs/{run_id}",
        duration_seconds=12.5,
        raw_payload={"id": run_id, "status": "completed", "conclusion": conclusion},
    )


def test_immediate_resolution_before_wait() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        event = tracker.register(101)
        assert not event.is_set()
        assert tracker.is_tracked(101)
        assert tracker.get(101) is None

        expected = _make_result(101)
        assert tracker.resolve(expected) is True
        assert event.is_set()
        assert tracker.get(101) == expected

        actual = await tracker.wait(101, timeout=1.0)
        assert actual == expected

    asyncio.run(scenario())


def test_register_is_idempotent_and_wakes_active_waiter() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        event1 = tracker.register(202)
        event2 = tracker.register(202)
        assert event1 is event2

        expected = _make_result(202, conclusion="failure")

        async def resolver() -> None:
            await asyncio.sleep(0.01)
            assert tracker.resolve(expected) is True

        waiter_task = asyncio.create_task(tracker.wait(202, timeout=1.0))
        resolver_task = asyncio.create_task(resolver())
        actual = await waiter_task
        await resolver_task

        assert actual == expected

    asyncio.run(scenario())


def test_wait_auto_registers_unregistered_run_id() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        expected = _make_result(303)

        async def resolver() -> None:
            await asyncio.sleep(0.01)
            assert tracker.resolve(expected) is True

        waiter_task = asyncio.create_task(tracker.wait(303, timeout=1.0))
        resolver_task = asyncio.create_task(resolver())
        actual = await waiter_task
        await resolver_task

        assert actual == expected

    asyncio.run(scenario())


def test_concurrent_waiters_all_receive_result() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        tracker.register(404)
        expected = _make_result(404)

        waiters = [asyncio.create_task(tracker.wait(404, timeout=1.0)) for _ in range(5)]
        await asyncio.sleep(0.01)
        assert tracker.resolve(expected) is True

        results = await asyncio.gather(*waiters)
        assert results == [expected] * 5

    asyncio.run(scenario())


def test_resolve_unknown_and_already_completed_is_safe() -> None:
    tracker = RunTracker()

    # Unknown run_id does not raise and is not stored.
    assert tracker.resolve(_make_result(999)) is False
    assert not tracker.is_tracked(999)
    assert tracker.get(999) is None

    # Already-completed run_id does not raise and keeps the first result.
    tracker.register(505)
    first = _make_result(505, conclusion="success")
    second = _make_result(505, conclusion="failure")
    assert tracker.resolve(first) is True
    assert tracker.resolve(second) is False
    assert tracker.get(505) == first


def test_wait_timeout_returns_none_and_allows_later_resolution() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        tracker.register(606)

        timed_out = await tracker.wait(606, timeout=0.01)
        assert timed_out is None

        expected = _make_result(606)
        assert tracker.resolve(expected) is True
        assert await tracker.wait(606, timeout=0.01) == expected

    asyncio.run(scenario())


def test_discard_clears_state_and_is_safe_for_unknown() -> None:
    tracker = RunTracker()
    tracker.discard(707)  # unknown run_id does not raise

    tracker.register(707)
    tracker.resolve(_make_result(707))
    assert tracker.is_tracked(707)

    tracker.discard(707)
    assert not tracker.is_tracked(707)
    assert tracker.get(707) is None


def _entry_of(tracker: RunTracker, run_id: int) -> _RunEntry:
    return tracker._entries[run_id]  # noqa: SLF001


def _wait_until_blocked(tracker: RunTracker, run_id: int, sync_waiters: int = 0, async_waiters: int = 0) -> None:
    """Spin until async waiters are enrolled and sync worker threads have started on ``run_id``."""
    deadline = time.monotonic() + 2.0
    entry = _entry_of(tracker, run_id)
    while time.monotonic() < deadline:
        parked_threads = sum(1 for t in threading.enumerate() if t.name.startswith(f"waiter-{run_id}") and t.is_alive())
        if len(entry.async_waiters) >= async_waiters and parked_threads >= sync_waiters:
            return
        time.sleep(0.001)
    msg = "waiters never blocked"
    raise AssertionError(msg)


def test_async_resolve_wakes_worker_thread_wait_sync_under_50ms() -> None:
    """A webhook-style coroutine resolving the run wakes a 60s worker-thread poll promptly."""
    tracker = RunTracker()
    tracker.register(808)
    expected = _make_result(808)
    started = threading.Event()
    woke_at: list[float] = []
    received: list[WorkflowRunResult | None] = []

    def poller() -> None:
        started.set()
        received.append(tracker.wait_sync(808, timeout=60.0))
        woke_at.append(time.perf_counter())

    async def webhook_handler() -> float:
        resolved_at = time.perf_counter()
        assert tracker.resolve(expected) is True
        return resolved_at

    async def scenario() -> None:
        worker = threading.Thread(target=poller, name="waiter-808")
        worker.start()
        await asyncio.to_thread(started.wait, 2.0)
        # Give the worker time to actually park inside Event.wait() rather than
        # racing into the already-resolved fast path.
        await asyncio.sleep(0.05)
        assert worker.is_alive()
        assert not woke_at

        resolved_at = await webhook_handler()
        await asyncio.to_thread(worker.join, 2.0)

        assert not worker.is_alive()
        assert received == [expected]
        assert woke_at[0] - resolved_at < 0.05

    asyncio.run(scenario())


def test_worker_thread_resolve_wakes_async_wait_under_50ms() -> None:
    """resolve() from a worker thread hops onto the waiter's event loop promptly."""

    async def scenario() -> None:
        tracker = RunTracker()
        expected = _make_result(809)
        waiter = asyncio.create_task(tracker.wait(809, timeout=60.0))
        await asyncio.sleep(0.01)
        assert len(_entry_of(tracker, 809).async_waiters) == 1

        resolved_at: list[float] = []

        def resolve_from_thread() -> None:
            # Let the event loop go idle in its selector first, so only a thread-safe
            # wake-up (not a lucky race) can deliver the result.
            time.sleep(0.05)
            resolved_at.append(time.perf_counter())
            assert tracker.resolve(expected) is True

        thread = threading.Thread(target=resolve_from_thread)
        thread.start()
        actual = await waiter
        woke_at = time.perf_counter()
        thread.join()

        assert actual == expected
        assert woke_at - resolved_at[0] < 0.05
        assert _entry_of(tracker, 809).async_waiters == []

    asyncio.run(scenario())


def test_wait_sync_returns_none_on_timeout_and_allows_later_resolution() -> None:
    tracker = RunTracker()
    tracker.register(810)

    start = time.perf_counter()
    assert tracker.wait_sync(810, timeout=0.02) is None
    assert time.perf_counter() - start >= 0.02
    assert tracker.is_tracked(810)

    expected = _make_result(810)
    assert tracker.resolve(expected) is True
    assert tracker.wait_sync(810, timeout=0.02) == expected


def test_wait_sync_returns_pre_resolved_result_immediately() -> None:
    tracker = RunTracker()
    tracker.register(811)
    expected = _make_result(811)
    assert tracker.resolve(expected) is True

    start = time.perf_counter()
    assert tracker.wait_sync(811, timeout=60.0) == expected
    assert time.perf_counter() - start < 0.05


def test_wait_sync_auto_registers_unregistered_run_id() -> None:
    tracker = RunTracker()
    expected = _make_result(812)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="waiter-812") as pool:
        future = pool.submit(tracker.wait_sync, 812, 5.0)
        deadline = time.monotonic() + 2.0
        while not tracker.is_tracked(812) and time.monotonic() < deadline:
            time.sleep(0.001)
        assert tracker.resolve(expected) is True
        assert future.result(timeout=2.0) == expected


def test_concurrent_sync_and_async_waiters_all_wake_on_one_resolve() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        tracker.register(813)
        expected = _make_result(813)

        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="waiter-813") as pool:
            loop = asyncio.get_running_loop()
            sync_waiters = [loop.run_in_executor(pool, tracker.wait_sync, 813, 60.0) for _ in range(4)]
            async_waiters = [asyncio.create_task(tracker.wait(813, timeout=60.0)) for _ in range(3)]
            await asyncio.sleep(0)
            await asyncio.to_thread(_wait_until_blocked, tracker, 813, 4, 3)
            await asyncio.sleep(0.02)

            assert tracker.resolve(expected) is True
            async with asyncio.timeout(2.0):
                results = await asyncio.gather(*sync_waiters, *async_waiters)

        assert results == [expected] * 7

    asyncio.run(scenario())


def test_concurrent_resolvers_from_many_threads_resolve_exactly_once() -> None:
    tracker = RunTracker()
    tracker.register(814)
    candidates = [_make_result(814, conclusion=f"c{i}") for i in range(16)]
    barrier = threading.Barrier(len(candidates))

    def race(result: WorkflowRunResult) -> bool:
        barrier.wait()
        return tracker.resolve(result)

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        outcomes = list(pool.map(race, candidates))

    assert outcomes.count(True) == 1
    winner = candidates[outcomes.index(True)]
    assert tracker.get(814) == winner
    assert tracker.wait_sync(814, timeout=0.01) == winner


def test_async_wait_timeout_removes_its_waiter() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        assert await tracker.wait(815, timeout=0.01) is None
        assert _entry_of(tracker, 815).async_waiters == []

        task = asyncio.create_task(tracker.wait(815, timeout=60.0))
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert _entry_of(tracker, 815).async_waiters == []

    asyncio.run(scenario())


def test_discard_wakes_pending_waiters_with_none() -> None:
    async def scenario() -> None:
        tracker = RunTracker()
        tracker.register(816)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="waiter-816") as pool:
            loop = asyncio.get_running_loop()
            sync_waiter = loop.run_in_executor(pool, tracker.wait_sync, 816, 60.0)
            async_waiter = asyncio.create_task(tracker.wait(816, timeout=60.0))
            await asyncio.sleep(0)
            await asyncio.to_thread(_wait_until_blocked, tracker, 816, 1, 1)
            await asyncio.sleep(0.02)

            tracker.discard(816)
            async with asyncio.timeout(2.0):
                results = await asyncio.gather(sync_waiter, async_waiter)

        assert list(results) == [None, None]
        assert not tracker.is_tracked(816)

    asyncio.run(scenario())


def test_discard_then_reregister_starts_fresh_entry() -> None:
    tracker = RunTracker()
    old_event = tracker.register(817)
    assert tracker.resolve(_make_result(817, conclusion="failure")) is True
    tracker.discard(817)
    assert not tracker.is_tracked(817)
    assert tracker.resolve(_make_result(817)) is False

    new_event = tracker.register(817)
    assert new_event is not old_event
    assert not new_event.is_set()
    assert tracker.get(817) is None
    assert tracker.wait_sync(817, timeout=0.01) is None

    fresh = _make_result(817, conclusion="success")
    assert tracker.resolve(fresh) is True
    assert tracker.wait_sync(817, timeout=0.01) == fresh


def test_resolve_tolerates_waiter_whose_loop_has_closed() -> None:
    tracker = RunTracker()
    loop = asyncio.new_event_loop()
    future: asyncio.Future[WorkflowRunResult | None] = loop.create_future()
    tracker.register(818)
    _entry_of(tracker, 818).async_waiters.append((loop, future))
    loop.close()

    expected = _make_result(818)
    assert tracker.resolve(expected) is True
    assert tracker.wait_sync(818, timeout=0.01) == expected
