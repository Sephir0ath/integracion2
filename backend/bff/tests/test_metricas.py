"""Pruebas de las metricas del BFF.

Los nombres son los del contrato compartido (los leen los paneles de
Grafana y los scripts de evidencia):

* `bff_requests_total{route,method,status}`;
* `bff_request_duration_seconds{route}`.

La etiqueta `route` lleva la **plantilla** de la ruta, no la ruta concreta:
asi el numero de series no crece con cada identificacion de saga.
"""

from __future__ import annotations

from common.metrics import REGISTRY

CABECERA = {"X-Client-Id": "cliente-1"}


def valor_contador(nombre: str, etiquetas: dict | None = None) -> float:
    """Valor actual de un contador del registro de `common.metrics`."""
    return REGISTRY.get_sample_value(nombre, etiquetas) or 0.0


async def test_cada_peticion_se_cuenta_en_la_metrica_del_bff(http):
    """La peticion atendida queda en `bff_requests_total`."""
    antes = valor_contador(
        "bff_requests_total",
        {"route": "/bff/web/catalogo", "method": "GET", "status": "200"},
    )

    await http.get("/bff/web/catalogo", headers=CABECERA)

    despues = valor_contador(
        "bff_requests_total",
        {"route": "/bff/web/catalogo", "method": "GET", "status": "200"},
    )
    assert despues == antes + 1


async def test_los_archivos_de_la_interfaz_se_agrupan_en_una_serie(http):
    """La interfaz web se mide como `/interfaz`, no como peticiones sueltas."""
    await http.get("/")

    assert valor_contador(
        "bff_requests_total",
        {"route": "/interfaz", "method": "GET", "status": "200"},
    ) >= 1


async def test_las_peticiones_que_fallan_tambien_se_cuentan(http):
    """Un 401 tambien queda en la metrica, con su estado."""
    await http.get("/bff/web/catalogo")

    assert (
        valor_contador(
            "bff_requests_total",
            {"route": "/bff/web/catalogo", "method": "GET", "status": "401"},
        )
        >= 1
    )
