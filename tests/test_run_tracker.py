import asyncio

from pit_crew.run_tracker import RunTracker, WorkflowRunResult


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
