"""Pruebas de los endpoints que no dependen de ningun servicio.

`/healthz` es el liveness que consulta el healthcheck del compose y
`/metrics` es el texto Prometheus que lee el panel de evidencia. Ninguno de
los dos toca una dependencia: para que el BFF se pueda diagnosticar
precisamente cuando el sistema esta caido.
"""

from __future__ import annotations


async def test_healthz_responde(http):
    """El liveness del BFF responde."""
    respuesta = await http.get("/healthz")

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["status"] == "ok"
    assert cuerpo["service"] == "bff"


async def test_metrics_responde_con_el_texto_prometheus(http):
    """Las metricas del BFF se publican en texto Prometheus."""
    respuesta = await http.get("/metrics")

    assert respuesta.status_code == 200
    assert "bff_requests_total" in respuesta.text
    assert "bff_request_duration_seconds" in respuesta.text


async def test_los_endpoints_publicos_no_exigen_la_identidad_del_cliente(http):
    """`/healthz` y `/metrics` son publicos, como la interfaz."""
    assert (await http.get("/healthz")).status_code == 200
    assert (await http.get("/metrics")).status_code == 200
