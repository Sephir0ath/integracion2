"""Pruebas de la salud, las metricas y el fallo de una dependencia.

Las tres son la evidencia de que el gateway se comporta como una puerta
fiable: responde aunque las dependencias esten caidas, cuenta lo que pasa
y no esconde un timeout detras de un 500 opaco.
"""

from __future__ import annotations

import grpc
import pytest

from common.metrics import REGISTRY

from .conftest import valor_contador
from .fakes import error_grpc, error_grpc_con_codigo


def series_por_nombre() -> dict[str, set[tuple[str, ...]]]:
    """Nombre de cada serie del registro -> etiquetas de sus muestras.

    Sirve para comprobar el **nombre y el juego de etiquetas** de las
    series del contrato: un contador sin etiquetas siempre tiene
    muestra, asi que preguntar solo por la existencia no dice nada.
    """
    series: dict[str, set[tuple[str, ...]]] = {}
    for familia in REGISTRY.collect():
        for muestra in familia.samples:
            series.setdefault(muestra.name, set()).add(tuple(sorted(muestra.labels)))
    return series


# ------------------------------------------------------------- la salud --


async def test_healthz_responde(http):
    """El liveness del gateway es publico."""
    respuesta = await http.get("/healthz")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["service"] == "gateway"


async def test_metrics_responde_en_formato_prometheus(http):
    """`/metrics` devuelve el texto Prometheus del registro compartido."""
    respuesta = await http.get("/metrics")

    assert respuesta.status_code == 200
    texto = respuesta.text
    assert "gateway_requests_total" in texto
    assert "gateway_auth_failures_total" in texto
    assert "gateway_rate_limit_rejected_total" in texto


async def test_las_series_del_contrato_existen_con_sus_etiquetas(http):
    """Los contadores y el histograma del contrato existen con sus etiquetas exactas.

    La asercion anterior (`get_sample_value(...) is not None`) era debil:
    un contador sin etiquetas siempre tiene muestra, asi que no
    comprobaba ni el nombre de la serie ni sus etiquetas. Ahora se
    comprueba el contrato completo:

    * `gateway_requests_total{route,method,status}`;
    * `gateway_request_duration_seconds{route}`;
    * `gateway_rate_limit_rejected_total`;
    * `gateway_auth_failures_total`.
    """
    respuesta = await http.get("/metrics")
    series = series_por_nombre()
    familias = {familia.name: familia for familia in REGISTRY.collect()}

    # Los contadores con etiquetas: el nombre del contrato y su juego de
    # etiquetas, ni una mas ni una menos.
    assert series["gateway_requests_total"] == {("method", "route", "status")}
    assert series["gateway_rate_limit_rejected_total"] == {()}
    assert series["gateway_auth_failures_total"] == {()}
    # El histograma del contrato: la etiqueta `route` y solo esa.
    assert familias["gateway_request_duration_seconds"].type == "histogram"
    assert series["gateway_request_duration_seconds_count"] == {("route",)}
    assert series["gateway_request_duration_seconds_sum"] == {("route",)}

    # Y los nombres del contrato aparecen tal cual en la exposicion.
    texto = respuesta.text
    assert "gateway_requests_total{" in texto
    assert "gateway_request_duration_seconds_bucket{" in texto
    assert "gateway_rate_limit_rejected_total " in texto
    assert "gateway_auth_failures_total " in texto

    # Las series de las rutas del contrato nacen en cero: las inicializa
    # el modulo de metricas al arrancar, antes de la primera peticion.
    assert (
        REGISTRY.get_sample_value(
            "gateway_requests_total",
            {"route": "/api/v1/entradas", "method": "GET", "status": "200"},
        )
        is not None
    )
    assert (
        REGISTRY.get_sample_value(
            "gateway_request_duration_seconds_count",
            {"route": "/api/v1/entradas"},
        )
        is not None
    )


async def test_las_metricas_cuentan_las_peticiones(http, tokens):
    """Cada peticion deja su rastro en `gateway_requests_total`."""
    antes = valor_contador(
        "gateway_requests_total",
        {"route": "/api/v1/butacas/eventos", "method": "GET", "status": "200"},
    )

    await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert (
        valor_contador(
            "gateway_requests_total",
            {"route": "/api/v1/butacas/eventos", "method": "GET", "status": "200"},
        )
        == antes + 1
    )


# ------------------------------------------------------ el limite de tiempo --


@pytest.mark.parametrize(
    "ruta",
    [
        "/api/v1/butacas/eventos",
        "/api/v1/butacas/secciones",
        "/api/v1/butacas/secciones/sec-1/butacas",
    ],
)
async def test_toda_llamada_grpc_lleva_su_deadline(http, stubs, settings, tokens, ruta):
    """Cada llamada al servicio interno lleva el limite de tiempo configurado."""
    await http.get(ruta, headers={"Authorization": f"Bearer {tokens.cliente()}"})

    llamada = stubs["butacas"].ultima_llamada
    assert llamada["timeout"] == settings.grpc_deadline


async def test_la_compra_tambien_lleva_su_deadline(http, stubs, settings, tokens):
    """La llamada al orquestador de la saga tambien lleva su deadline."""
    await http.post(
        "/api/v1/compras",
        json={"saga_id": "saga-1", "evento_id": "ev-1", "seccion_id": "sec-1", "butaca_ids": ["sec-1-A1"]},
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert stubs["compras"].ultima_llamada["timeout"] == settings.grpc_deadline


# ------------------------------------------------- la dependencia caida --


async def test_dependencia_caida_devuelve_503_con_codigo(http, stubs, tokens):
    """Un servicio caido se reporta con su codigo, no con un 500 opaco."""
    stubs["butacas"].error = error_grpc_con_codigo("DEPENDENCY_UNAVAILABLE", "butacas no esta disponible")

    respuesta = await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 503
    cuerpo = respuesta.json()
    assert cuerpo["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "message" in cuerpo


async def test_timeout_de_la_dependencia_devuelve_503(http, stubs, tokens):
    """Un timeout es una dependencia caida, no un error del gateway."""
    stubs["emision"].error = TimeoutError("se agoto el tiempo")

    respuesta = await http.get(
        "/api/v1/entradas",
        params={"saga_id": "saga-1"},
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 503
    assert respuesta.json()["code"] == "DEPENDENCY_UNAVAILABLE"


async def test_grpc_caido_sin_codigo_devuelve_503(http, stubs, tokens):
    """Un status gRPC sin codigo de dominio tambien se traduce a 503."""
    stubs["compras"].error = error_grpc(grpc.StatusCode.UNAVAILABLE, "sin detalles")

    respuesta = await http.get(
        "/api/v1/compras/saga-1",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 503
    assert respuesta.json()["code"] == "DEPENDENCY_UNAVAILABLE"
