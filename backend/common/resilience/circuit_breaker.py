"""Circuit Breaker.

The Circuit Breaker isolates a dependency that is failing, so the caller
fails fast instead of waiting for a dependency that will not answer. That is
what keeps a cascading failure contained: the rest of the system keeps
serving traffic while the broken dependency is given time to recover.

States:

* ``CLOSED``    every call reaches the dependency. After `failure_threshold`
  consecutive failed calls the circuit opens.
* ``OPEN``      calls fail fast without touching the dependency. The fallback
  provides the degraded answer, which is the graceful degradation.
* ``HALF_OPEN`` after `reset_timeout` the circuit lets a limited number of
  probe calls through. A successful probe closes the circuit, a failed probe
  opens it again.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

from common.metrics import counter, gauge

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"

_state_gauge = gauge(
    "circuit_breaker_state",
    "State of the circuit breaker (1 for the active state, 0 for the others)",
    ["breaker", "state"],
)
_transitions = counter(
    "circuit_breaker_transitions_total",
    "Circuit breaker state transitions",
    ["breaker", "from_state", "to_state"],
)
_rejected = counter(
    "circuit_breaker_rejected_total",
    "Calls rejected without reaching the dependency",
    ["breaker"],
)


class CircuitOpenError(RuntimeError):
    """Raised when the circuit is open and no fallback was provided."""


async def _maybe_await(value: Any) -> Any:
    """Await a value when it is awaitable, so the fallback may be sync or async."""
    if inspect.isawaitable(value):
        return await value
    return value


class CircuitBreaker:
    """Guard around a fragile dependency."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        reset_timeout: float = 8.0,
        half_open_max_calls: int = 1,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")

        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = 0
        self._rejected_count = 0
        self._lock = asyncio.Lock()
        self._publish_state()

    @property
    def state(self) -> str:
        """Current state of the circuit."""
        return self._state

    def _publish_state(self) -> None:
        for state in (CLOSED, OPEN, HALF_OPEN):
            _state_gauge.labels(self.name, state).set(1 if state == self._state else 0)

    def _transition(self, new_state: str) -> None:
        previous = self._state
        self._state = new_state
        self._publish_state()
        _transitions.labels(self.name, previous, new_state).inc()

    async def call(
        self,
        fn: Callable[[], Awaitable[Any]],
        fallback: Callable[[], Any] | None = None,
    ) -> Any:
        """Run `fn` through the circuit.

        `fallback` may be sync or async. It is used when the circuit is open
        (the degraded answer) and when the call fails exactly while the
        circuit opens (the dependency just broke).
        """
        if not await self._allow():
            self._rejected_count += 1
            _rejected.labels(self.name).inc()
            if fallback is not None:
                return await _maybe_await(fallback())
            raise CircuitOpenError(f"circuit '{self.name}' is open")

        try:
            result = await fn()
        except Exception:
            opened = await self._on_failure()
            if opened and fallback is not None:
                return await _maybe_await(fallback())
            raise

        await self._on_success()
        return result

    async def _allow(self) -> bool:
        """Decide whether the call may reach the dependency."""
        async with self._lock:
            if self._state == CLOSED:
                return True

            if self._state == OPEN:
                if time.monotonic() - self._opened_at < self.reset_timeout:
                    return False
                self._transition(HALF_OPEN)
                self._half_open_in_flight = 0

            if self._state == HALF_OPEN:
                if self._half_open_in_flight >= self.half_open_max_calls:
                    return False
                self._half_open_in_flight += 1
            return True

    async def _on_success(self) -> None:
        async with self._lock:
            self._failures = 0
            if self._state == HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._transition(CLOSED)

    async def _on_failure(self) -> bool:
        """Record a failure. Returns True when the circuit just opened."""
        async with self._lock:
            if self._state == HALF_OPEN:
                self._half_open_in_flight = max(0, self._half_open_in_flight - 1)
                self._failures = self.failure_threshold
                self._opened_at = time.monotonic()
                self._transition(OPEN)
                return True

            self._failures += 1
            if self._state == CLOSED and self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                self._transition(OPEN)
                return True
            return False

    def snapshot(self) -> dict:
        """Observable state of the circuit, used by the BFF and the demo."""
        return {
            "breaker": self.name,
            "estado": self._state,
            "fallos_consecutivos": self._failures,
            "llamadas_rechazadas": self._rejected_count,
            "failure_threshold": self.failure_threshold,
            "reset_timeout": self.reset_timeout,
        }
