"""HTTP application of the simulated payment gateway.

Endpoints:

* ``POST /pagos``                    charge an amount (idempotent by key).
* ``POST /pagos/{pago_id}/reembolso`` refund a payment.
* ``GET  /pagos/{pago_id}``          read a payment (observability).
* ``GET  /healthz``                  liveness.
* ``GET  /metrics``                  Prometheus text format.
* ``POST /admin/fault``              inject a fault (`ok`, `slow`, `down`).
* ``GET  /admin/fault``              read the fault that is applied now.
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from common.logging import log_event
from common.metrics import REGISTRY, counter, histogram

from .faults import FalloPasarela, FaultController, ModoInvalido
from .store import CONFIRMADO, REEMBOLSADO, PasarelaStore

logger = logging.getLogger("pasarela")

_solicitudes = counter(
    "pasarela_solicitudes_total",
    "Llamadas recibidas por la pasarela simulada",
    ["endpoint", "resultado"],
)
_latencia = histogram(
    "pasarela_latencia_segundos",
    "Duración de las llamadas atendidas por la pasarela simulada",
)


class CobrarBody(BaseModel):
    """Body of `POST /pagos`."""

    monto_centavos: int
    idempotency_key: str


class FaultBody(BaseModel):
    """Body of `POST /admin/fault`."""

    mode: str = "ok"
    delay_ms: int = 0
    fail_rate: float = 0.0


def create_app() -> FastAPI:
    """Build the FastAPI application of the simulated gateway."""
    app = FastAPI(title="Pasarela (simulador)", docs_url=None, redoc_url=None)

    store = PasarelaStore()
    faults = FaultController()
    app.state.store = store
    app.state.faults = faults

    async def _atender(endpoint: str, operacion):
        """Apply the fault, run the operation and account the call."""
        inicio = time.perf_counter()
        try:
            await faults.aplicar()
        except FalloPasarela as exc:
            _solicitudes.labels(endpoint, "error").inc()
            _latencia.observe(time.perf_counter() - inicio)
            log_event(logger, "pasarela_fallo_inyectado", endpoint=endpoint, modo=exc.modo)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        resultado = operacion()
        _solicitudes.labels(endpoint, "ok").inc()
        _latencia.observe(time.perf_counter() - inicio)
        return resultado

    @app.post("/pagos")
    async def cobrar(cuerpo: CobrarBody):
        """Charge an amount. The same `idempotency_key` charges only once."""
        if cuerpo.monto_centavos < 0:
            raise HTTPException(status_code=400, detail="monto_centavos no puede ser negativo")

        registro = await _atender(
            "pagos", lambda: store.cobrar(cuerpo.monto_centavos, cuerpo.idempotency_key)
        )
        log_event(
            logger,
            "pasarela_cobro",
            pago_id=registro["pago_id"],
            monto_centavos=registro["monto_centavos"],
            idempotency_key=cuerpo.idempotency_key,
        )
        return {
            "pago_id": registro["pago_id"],
            "estado": CONFIRMADO,
            "referencia": registro["referencia"],
        }

    @app.post("/pagos/{pago_id}/reembolso")
    async def reembolsar(pago_id: str):
        """Refund a payment."""
        registro = await _atender("reembolso", lambda: store.reembolsar(pago_id))
        if registro is None:
            _solicitudes.labels("reembolso", "no_encontrado").inc()
            raise HTTPException(status_code=404, detail=f"pago desconocido: {pago_id}")
        log_event(logger, "pasarela_reembolso", pago_id=pago_id)
        return {"estado": REEMBOLSADO, "pago_id": pago_id, "referencia": registro["referencia"]}

    @app.get("/pagos/{pago_id}")
    async def consultar(pago_id: str):
        """Read a payment."""
        registro = store.obtener(pago_id)
        if registro is None:
            raise HTTPException(status_code=404, detail=f"pago desconocido: {pago_id}")
        return registro

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "service": "pasarela",
            "modo": faults.modo,
            "cobros": store.cobros,
            "reembolsos": store.reembolsos,
        }

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.post("/admin/fault")
    async def configurar_fault(cuerpo: FaultBody):
        """Inject a fault into the payment endpoints."""
        try:
            estado = faults.configurar(cuerpo.mode, cuerpo.delay_ms, cuerpo.fail_rate)
        except ModoInvalido as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log_event(logger, "pasarela_fault_configurado", **estado)
        return estado

    @app.get("/admin/fault")
    async def leer_fault():
        """Read the fault that is applied right now."""
        return faults.estado()

    @app.get("/")
    async def raiz():
        return JSONResponse(
            {
                "servicio": "pasarela",
                "descripcion": "simulador de la pasarela de pagos externa",
                "endpoints": ["/pagos", "/pagos/{pago_id}/reembolso", "/admin/fault"],
            }
        )

    return app
