"""Tests of the Circuit Breaker: the pattern that contains the fragile dependency."""

import asyncio

import pytest
from prometheus_client import generate_latest

from common.metrics import REGISTRY
from common.resilience.circuit_breaker import (
    CLOSED,
    OPEN,
    CircuitBreaker,
    CircuitOpenError,
)


async def _ok():
    return "ok"


async def _boom():
    raise RuntimeError("dependency down")


@pytest.mark.asyncio
async def test_starts_closed():
    breaker = CircuitBreaker("pasarela", failure_threshold=2, reset_timeout=0.05)
    assert breaker.state == CLOSED
    assert breaker.snapshot()["estado"] == CLOSED


@pytest.mark.asyncio
async def test_opens_after_threshold_consecutive_failures():
    breaker = CircuitBreaker("pasarela", failure_threshold=3, reset_timeout=0.05)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_boom)
    assert breaker.state == CLOSED

    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.state == OPEN


@pytest.mark.asyncio
async def test_open_circuit_fails_fast_without_calling_the_dependency():
    breaker = CircuitBreaker("pasarela", failure_threshold=1, reset_timeout=60)
    calls = []

    async def dependency():
        calls.append(1)
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        await breaker.call(dependency)
    assert breaker.state == OPEN

    with pytest.raises(CircuitOpenError):
        await breaker.call(dependency)

    assert len(calls) == 1, "the dependency must not be called again while the circuit is open"


@pytest.mark.asyncio
async def test_open_circuit_uses_the_fallback():
    breaker = CircuitBreaker("pasarela", failure_threshold=1, reset_timeout=60)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)

    assert await breaker.call(_boom, fallback=lambda: "degradado") == "degradado"


@pytest.mark.asyncio
async def test_fallback_may_be_asynchronous():
    breaker = CircuitBreaker("pasarela", failure_threshold=1, reset_timeout=60)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)

    async def fallback():
        return "degradado-async"

    assert await breaker.call(_boom, fallback=fallback) == "degradado-async"


@pytest.mark.asyncio
async def test_half_open_after_reset_timeout_and_closes_on_success():
    breaker = CircuitBreaker("pasarela", failure_threshold=1, reset_timeout=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.state == OPEN

    await asyncio.sleep(0.06)

    assert await breaker.call(_ok) == "ok"
    assert breaker.state == CLOSED


@pytest.mark.asyncio
async def test_half_open_reopens_after_a_failed_probe():
    breaker = CircuitBreaker("pasarela", failure_threshold=1, reset_timeout=0.05)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)

    await asyncio.sleep(0.06)

    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.state == OPEN


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_counter():
    breaker = CircuitBreaker("pasarela", failure_threshold=2, reset_timeout=60)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.snapshot()["fallos_consecutivos"] == 1

    await breaker.call(_ok)
    assert breaker.snapshot()["fallos_consecutivos"] == 0

    with pytest.raises(RuntimeError):
        await breaker.call(_boom)
    assert breaker.state == CLOSED, "the counter restarted, so one failure is not enough to open it"


@pytest.mark.asyncio
async def test_metrics_publish_the_current_state():
    breaker = CircuitBreaker("pasarela-metricas", failure_threshold=1, reset_timeout=60)
    with pytest.raises(RuntimeError):
        await breaker.call(_boom)

    payload = generate_latest(REGISTRY).decode()
    lines = [
        line
        for line in payload.splitlines()
        if line.startswith("circuit_breaker_state{") and "pasarela-metricas" in line
    ]
    assert any('state="OPEN"' in line and line.endswith(" 1.0") for line in lines)
    assert any('state="CLOSED"' in line and line.endswith(" 0.0") for line in lines)
