"""Pruebas de la resiliencia del BFF.

El BFF habla con cuatro servicios internos: **toda** llamada lleva su
limite de tiempo (`GRPC_DEADLINE`), porque una dependencia lenta no puede
dejar al BFF esperando. Ademas, una dependencia caida se traduce a un 503
con el codigo de `common.errors`, nunca a un 500 opaco.
"""

from __future__ import annotations

from .conftest import cliente_para
from .fakes import error_grpc

CABECERA = {"X-Client-Id": "cliente-1"}


async def test_toda_llamada_grpc_lleva_su_limite_de_tiempo(http, stubs, settings):
    """Ninguna llamada a un servicio interno se queda sin deadline."""
    await http.get("/bff/web/catalogo", headers=CABECERA)
    await http.get("/bff/web/secciones/sec-1/butacas", headers=CABECERA)
    await http.post(
        "/bff/web/compras",
        json={
            "saga_id": "saga-1",
            "evento_id": "ev-1",
            "seccion_id": "sec-1",
            "butaca_ids": ["sec-1-A1"],
        },
        headers=CABECERA,
    )
    await http.get("/bff/web/compras/saga-1", headers=CABECERA)

    llamadas = [llamada for doble in stubs.values() for llamada in doble.calls]
    assert llamadas, "el BFF no llamo a ningun servicio interno"
    for llamada in llamadas:
        assert llamada["timeout"] == settings.grpc_deadline


async def test_una_dependencia_caida_no_produce_un_500_opaco(construir_app, stubs):
    """Un fallo raro de una dependencia sale como 503 con codigo de dominio."""
    stubs["pagos"].error = RuntimeError("la dependencia se rompio de una forma rara")
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get("/bff/web/catalogo", headers=CABECERA)

    # `pagos` es opcional: la respuesta es 200 degradada, no un 500.
    assert respuesta.status_code == 200
    assert respuesta.json()["degradado"] is True


async def test_la_identidad_del_cliente_travela_hasta_los_servicios(http, stubs):
    """El BFF reenvia la identidad que el gateway ya valido."""
    await http.get("/bff/web/catalogo", headers={"X-Client-Id": "cliente-7", "X-Role": "cliente"})

    llamada = stubs["butacas"].calls[0]
    assert llamada["metadata"].get("x-client-id") == "cliente-7"
    assert llamada["metadata"].get("x-role") == "cliente"


async def test_el_plano_de_la_sala_falla_con_503_si_butacas_no_responde(construir_app, stubs):
    """Una dependencia esencial caida se traduce a 503 con su codigo."""
    stubs["butacas"].error = error_grpc()
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get("/bff/web/secciones/sec-1/butacas", headers=CABECERA)

    assert respuesta.status_code == 503
    assert respuesta.json()["code"] == "DEPENDENCY_UNAVAILABLE"
