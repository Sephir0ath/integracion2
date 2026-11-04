"""Tests of the gRPC helpers."""

import pytest
from fastapi.testclient import TestClient

from common.grpc import build_admin_app, unary
from common.resilience.bulkhead import Bulkhead
from common.resilience.circuit_breaker import CircuitBreaker


def test_admin_app_serves_the_healthcheck():
    app = build_admin_app("servicio-prueba", health_fn=lambda: {"db": True})
    client = TestClient(app)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["service"] == "servicio-prueba"
    assert response.json()["status"] == "ok"
    assert response.json()["db"] is True


def test_admin_app_reports_a_degraded_healthcheck():
    def broken():
        raise RuntimeError("db caida")

    client = TestClient(build_admin_app("servicio-prueba", health_fn=broken))
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_admin_app_serves_the_metrics():
    client = TestClient(build_admin_app("servicio-prueba"))
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "# HELP" in response.text


@pytest.mark.asyncio
async def test_unary_calls_the_stub_with_the_deadline():
    seen = {}

    async def stub(request, timeout):
        seen["timeout"] = timeout
        seen["request"] = request
        return "respuesta"

    result = await unary(None, stub, {"a": 1}, timeout=2.5)

    assert result == "respuesta"
    assert seen["timeout"] == 2.5
    assert seen["request"] == {"a": 1}


@pytest.mark.asyncio
async def test_unary_retries_inside_the_breaker():
    attempts = []

    async def stub(request, timeout):
        attempts.append(1)
        raise RuntimeError("boom")

    breaker = CircuitBreaker("prueba-grpc", failure_threshold=5, reset_timeout=60)

    with pytest.raises(RuntimeError):
        await unary(None, stub, {}, timeout=1.0, retries=2, breaker=breaker)

    assert len(attempts) == 3, "one call plus two retries"


@pytest.mark.asyncio
async def test_unary_does_not_retry_while_the_circuit_is_open():
    attempts = []

    async def stub(request, timeout):
        attempts.append(1)
        raise RuntimeError("boom")

    breaker = CircuitBreaker("prueba-grpc-abierto", failure_threshold=1, reset_timeout=60)

    with pytest.raises(RuntimeError):
        await unary(None, stub, {}, timeout=1.0, retries=2, breaker=breaker)
    assert len(attempts) == 3

    attempts.clear()
    with pytest.raises(Exception):
        await unary(None, stub, {}, timeout=1.0, retries=2, breaker=breaker)

    assert len(attempts) == 0, "an open circuit fails fast without retrying"


@pytest.mark.asyncio
async def test_unary_uses_the_bulkhead():
    bulkhead = Bulkhead("prueba-bulkhead", max_concurrent=1, max_pending=0)

    async def stub(request, timeout):
        return "ok"

    assert await unary(None, stub, {}, timeout=1.0, bulkhead=bulkhead) == "ok"
