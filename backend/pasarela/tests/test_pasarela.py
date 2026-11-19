"""Tests of the simulated payment gateway.

The application is exercised through an ASGI transport, so no port is bound
and no external dependency is needed.
"""

from __future__ import annotations

import time

import httpx
import pytest

from ..app import create_app


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
async def cliente(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://pasarela.test") as http:
        yield http


async def test_cobrar_devuelve_el_pago_confirmado(cliente):
    respuesta = await cliente.post(
        "/pagos", json={"monto_centavos": 25000, "idempotency_key": "saga-1:COBRAR"}
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "CONFIRMADO"
    assert cuerpo["pago_id"].startswith("pgw_")
    assert cuerpo["referencia"].startswith("REF-")


async def test_la_misma_clave_de_idempotencia_cobra_una_sola_vez(app, cliente):
    primera = await cliente.post(
        "/pagos", json={"monto_centavos": 25000, "idempotency_key": "saga-1:COBRAR"}
    )
    segunda = await cliente.post(
        "/pagos", json={"monto_centavos": 25000, "idempotency_key": "saga-1:COBRAR"}
    )

    assert primera.json()["pago_id"] == segunda.json()["pago_id"]
    assert primera.json()["referencia"] == segunda.json()["referencia"]

    # The ledger of the simulator has a single payment for that key.
    assert len(app.state.store.pagos()) == 1
    assert app.state.store.cobros == 1


async def test_claves_distintas_cobran_pagos_distintos(cliente):
    primera = await cliente.post("/pagos", json={"monto_centavos": 1000, "idempotency_key": "a"})
    segunda = await cliente.post("/pagos", json={"monto_centavos": 1000, "idempotency_key": "b"})

    assert primera.json()["pago_id"] != segunda.json()["pago_id"]


async def test_el_reembolso_marca_el_pago_como_reembolsado(cliente):
    cobro = await cliente.post("/pagos", json={"monto_centavos": 25000, "idempotency_key": "saga-1"})
    pago_id = cobro.json()["pago_id"]

    reembolso = await cliente.post(f"/pagos/{pago_id}/reembolso")

    assert reembolso.status_code == 200
    assert reembolso.json()["estado"] == "REEMBOLSADO"

    consulta = await cliente.get(f"/pagos/{pago_id}")
    assert consulta.json()["estado"] == "REEMBOLSADO"


async def test_el_reembolso_de_un_pago_desconocido_es_404(cliente):
    respuesta = await cliente.post("/pagos/pgw_no_existe/reembolso")
    assert respuesta.status_code == 404


async def test_el_modo_down_hace_fallar_el_cobro(app, cliente):
    configuracion = await cliente.post("/admin/fault", json={"mode": "down", "delay_ms": 0, "fail_rate": 0.0})
    assert configuracion.status_code == 200
    assert configuracion.json()["mode"] == "down"

    respuesta = await cliente.post("/pagos", json={"monto_centavos": 1000, "idempotency_key": "x"})

    assert respuesta.status_code == 503
    # A failed call does not charge anything.
    assert app.state.store.cobros == 0


async def test_el_modo_slow_retrasa_el_cobro(cliente):
    await cliente.post("/admin/fault", json={"mode": "slow", "delay_ms": 300, "fail_rate": 0.0})

    inicio = time.perf_counter()
    respuesta = await cliente.post("/pagos", json={"monto_centavos": 1000, "idempotency_key": "lento"})
    duracion = time.perf_counter() - inicio

    assert respuesta.status_code == 200
    assert duracion >= 0.3, "el modo slow tiene que retrasar la respuesta"


async def test_el_modo_se_puede_leer_de_vuelta(cliente):
    await cliente.post("/admin/fault", json={"mode": "slow", "delay_ms": 1200, "fail_rate": 0.25})

    estado = await cliente.get("/admin/fault")

    assert estado.status_code == 200
    assert estado.json() == {"mode": "slow", "delay_ms": 1200, "fail_rate": 0.25}


async def test_un_modo_desconocido_se_rechaza(cliente):
    respuesta = await cliente.post("/admin/fault", json={"mode": "explosion"})
    assert respuesta.status_code == 400


async def test_el_control_de_fallas_no_afecta_a_los_endpoints_de_administracion(cliente):
    await cliente.post("/admin/fault", json={"mode": "down"})

    assert (await cliente.get("/admin/fault")).status_code == 200
    assert (await cliente.get("/healthz")).status_code == 200
    assert (await cliente.get("/metrics")).status_code == 200


async def test_healthz_y_metrics_responden(cliente):
    health = await cliente.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    metrics = await cliente.get("/metrics")
    assert metrics.status_code == 200
    assert "pasarela_solicitudes_total" in metrics.text


async def test_la_tasa_de_fallo_aleatoria_se_aplica(cliente):
    await cliente.post("/admin/fault", json={"mode": "ok", "delay_ms": 0, "fail_rate": 1.0})

    respuesta = await cliente.post("/pagos", json={"monto_centavos": 1000, "idempotency_key": "azar"})

    assert respuesta.status_code == 503
