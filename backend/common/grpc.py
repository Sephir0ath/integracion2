"""gRPC server and client helpers.

A service process exposes its gRPC API and, in the same process, an admin
HTTP port with `/healthz` and `/metrics` for the container healthcheck and
for Prometheus.

`unary` is the single exit point for every internal call, so the timeout and
the resilience patterns are applied in one place instead of being forgotten
in one service and applied in another.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import grpc
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from uvicorn import Config, Server

from common.metrics import REGISTRY

logger = logging.getLogger(__name__)


def create_channel(target: str) -> grpc.aio.Channel:
    """Open a channel to an internal service."""
    return grpc.aio.insecure_channel(target)


async def unary(
    channel,
    method,
    request,
    *,
    timeout: float,
    retries: int = 0,
    breaker=None,
    bulkhead=None,
    fallback=None,
) -> Any:
    """Call a unary RPC under a deadline and the resilience wrappers.

    The retry wrapper lives inside the breaker on purpose: while the circuit
    is open the breaker never invokes the wrapped function, so an open
    circuit fails fast instead of retrying against a dead dependency.
    """
    del channel  # the channel is bound to the stub passed as `method`

    async def _invoke():
        return await method(request, timeout=timeout)

    async def _guarded():
        if bulkhead is not None:
            return await bulkhead.run(_invoke)
        return await _invoke()

    async def _call():
        if retries > 0:
            from common.resilience.retry import with_retry

            return await with_retry(_guarded, attempts=retries + 1)
        return await _guarded()

    if breaker is not None:
        return await breaker.call(_call, fallback=fallback)
    return await _call()


def build_admin_app(service_name: str, health_fn: Callable[[], dict] | None = None) -> FastAPI:
    """Admin HTTP app with the healthcheck and the metrics endpoint."""
    app = FastAPI(title=f"{service_name} admin", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz():
        detail: dict = {}
        status = "ok"
        if health_fn is not None:
            try:
                detail = health_fn() or {}
            except Exception as exc:  # noqa: BLE001 - reported as degraded
                status = "degraded"
                detail = {"error": str(exc)}
        return JSONResponse({"status": status, "service": service_name, **detail})

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app


async def serve_grpc(
    service_name: str,
    port: int,
    admin_port: int,
    register_fn: Callable[[grpc.aio.Server], None],
    health_fn: Callable[[], dict] | None = None,
) -> None:
    """Run the gRPC server and the admin HTTP app until the process stops."""
    server = grpc.aio.server()
    register_fn(server)
    server.add_insecure_port(f"[::]:{port}")

    admin_server = Server(
        Config(
            build_admin_app(service_name, health_fn),
            host="0.0.0.0",
            port=admin_port,
            log_level="warning",
            access_log=False,
        )
    )

    await server.start()
    admin_task = asyncio.create_task(admin_server.serve())
    logger.info(
        "grpc_server_started",
        extra={"event_fields": {"service": service_name, "grpc_port": port, "admin_port": admin_port}},
    )
    try:
        await server.wait_for_termination()
    finally:
        await admin_server.shutdown()
        admin_task.cancel()


async def wait_for_call(coro: Awaitable[Any], timeout: float) -> Any:
    """Await a coroutine under a deadline, cancelling it when it expires."""
    return await asyncio.wait_for(coro, timeout=timeout)
