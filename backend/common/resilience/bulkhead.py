"""Bulkhead.

The Bulkhead isolates the resources spent on a fragile dependency. At most
`max_concurrent` calls run at the same time and at most `max_pending` more
wait in the queue; the excess is rejected immediately.

That is what keeps a slow dependency from exhausting the resources of the
rest of the system: the caller gets a fast, explicit rejection instead of a
process full of threads waiting for a dependency that will not answer.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from common.metrics import counter, gauge

_rejected = counter(
    "bulkhead_rejected_total",
    "Calls rejected because the bulkhead is saturated",
    ["bulkhead"],
)
_in_use = gauge(
    "bulkhead_in_use",
    "Calls running or queued in the bulkhead",
    ["bulkhead"],
)


class BulkheadRejected(RuntimeError):
    """Raised when the bulkhead has no room for another call."""


class Bulkhead:
    """Bounded concurrency around a fragile dependency."""

    def __init__(self, name: str, max_concurrent: int = 5, max_pending: int = 10) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_pending < 0:
            raise ValueError("max_pending must be >= 0")

        self.name = name
        self.max_concurrent = max_concurrent
        self.max_pending = max_pending

        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._admitted = 0
        self._rejected_count = 0
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[[], Awaitable[Any]]) -> Any:
        """Run `fn` inside the bulkhead."""
        async with self._lock:
            if self._admitted >= self.max_concurrent + self.max_pending:
                self._rejected_count += 1
                _rejected.labels(self.name).inc()
                raise BulkheadRejected(f"bulkhead '{self.name}' is saturated")
            self._admitted += 1
            _in_use.labels(self.name).set(self._admitted)

        try:
            async with self._semaphore:
                return await fn()
        finally:
            async with self._lock:
                self._admitted -= 1
                _in_use.labels(self.name).set(self._admitted)

    def snapshot(self) -> dict:
        """Observable state of the bulkhead, used by the BFF and the demo."""
        return {
            "bulkhead": self.name,
            "max_concurrent": self.max_concurrent,
            "max_pending": self.max_pending,
            "admitidos": self._admitted,
            "rechazos": self._rejected_count,
        }
