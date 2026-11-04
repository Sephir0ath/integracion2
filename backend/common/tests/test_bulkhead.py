"""Tests of the Bulkhead: the pattern that isolates the resources of a fragile dependency."""

import asyncio

import pytest

from common.resilience.bulkhead import Bulkhead, BulkheadRejected


@pytest.mark.asyncio
async def test_runs_the_call_and_returns_the_result():
    bulkhead = Bulkhead("pasarela", max_concurrent=2, max_pending=2)

    async def work():
        return "hecho"

    assert await bulkhead.run(work) == "hecho"


@pytest.mark.asyncio
async def test_rejects_the_excess_immediately():
    bulkhead = Bulkhead("pasarela", max_concurrent=1, max_pending=0)
    release = asyncio.Event()
    calls = []

    async def slow():
        calls.append(1)
        await release.wait()
        return "tarde"

    task = asyncio.create_task(bulkhead.run(slow))
    await asyncio.sleep(0.01)

    with pytest.raises(BulkheadRejected):
        await bulkhead.run(slow)

    release.set()
    assert await task == "tarde"
    assert len(calls) == 1, "the rejected call must not reach the dependency"


@pytest.mark.asyncio
async def test_queued_calls_wait_for_a_slot():
    bulkhead = Bulkhead("pasarela", max_concurrent=1, max_pending=1)

    async def work(tag):
        await asyncio.sleep(0.01)
        return tag

    results = await asyncio.gather(
        bulkhead.run(lambda: work("a")),
        bulkhead.run(lambda: work("b")),
    )
    assert results == ["a", "b"]


@pytest.mark.asyncio
async def test_releases_the_slot_after_a_failure():
    bulkhead = Bulkhead("pasarela", max_concurrent=1, max_pending=0)

    async def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await bulkhead.run(boom)

    assert bulkhead.snapshot()["admitidos"] == 0

    async def ok():
        return "ok"

    assert await bulkhead.run(ok) == "ok"


@pytest.mark.asyncio
async def test_counts_the_rejections():
    bulkhead = Bulkhead("pasarela", max_concurrent=1, max_pending=0)
    release = asyncio.Event()

    async def slow():
        await release.wait()

    task = asyncio.create_task(bulkhead.run(slow))
    await asyncio.sleep(0.01)

    for _ in range(3):
        with pytest.raises(BulkheadRejected):
            await bulkhead.run(slow)

    release.set()
    await task

    assert bulkhead.snapshot()["rechazos"] == 3
