"""Retry with exponential backoff.

The retry helper only covers transient failures. It is used inside the
Circuit Breaker, never outside it: while the circuit is open the breaker
never invokes the wrapped function, so an open circuit fails fast instead of
retrying against a dead dependency.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Sequence

from common.metrics import counter

_retries = counter("retries_total", "Retries performed by the retry helper", ["operation"])


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 2,
    base_delay: float = 0.1,
    max_delay: float = 1.0,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    operation: str = "generic",
) -> Any:
    """Run `fn` up to `attempts` times with exponential backoff.

    The last error is raised when every attempt fails.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")

    delay = base_delay
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except tuple(retry_on) as error:  # type: ignore[misc]
            last_error = error
            if attempt == attempts:
                break
            _retries.labels(operation).inc()
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)

    assert last_error is not None
    raise last_error
