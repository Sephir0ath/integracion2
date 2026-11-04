"""Tests of the retry helper."""

import time

import pytest

from common.resilience.retry import with_retry


@pytest.mark.asyncio
async def test_returns_without_retrying():
    attempts = []

    async def ok():
        attempts.append(1)
        return "ok"

    assert await with_retry(ok, attempts=3, base_delay=0.01) == "ok"
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_retries_until_success():
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "ok"

    assert await with_retry(flaky, attempts=3, base_delay=0.01) == "ok"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_raises_the_last_error_after_the_attempts():
    attempts = []

    async def always_fail():
        attempts.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await with_retry(always_fail, attempts=3, base_delay=0.01)

    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_backoff_grows_between_attempts():
    async def always_fail():
        raise RuntimeError("boom")

    started = time.monotonic()
    with pytest.raises(RuntimeError):
        await with_retry(always_fail, attempts=3, base_delay=0.05, max_delay=1.0)
    elapsed = time.monotonic() - started

    assert elapsed >= 0.05 + 0.10, "the second wait must be longer than the first"


@pytest.mark.asyncio
async def test_does_not_retry_unlisted_exceptions():
    attempts = []

    async def boom():
        attempts.append(1)
        raise ValueError("no retry")

    with pytest.raises(ValueError):
        await with_retry(boom, attempts=3, base_delay=0.01, retry_on=(RuntimeError,))

    assert len(attempts) == 1
