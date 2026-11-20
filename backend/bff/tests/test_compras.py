"""Pruebas de las rutas de compra.

La compra es la composicion de `compras` (la saga) y `emision` (las
entradas); la consulta de una saga suma `butacas` (la reserva). Ademas
estas rutas son las de negocio: exigen la identidad del cliente, que el
gateway ya valido y reenvio en `X-Client-Id`.
"""

from __future__ import annotations

import json

from .conftest import cliente_para
from .fakes import error_grpc

CABECERA = {"X-Client-Id": "cliente-1"}
CUERPO_COMPRA = {
    "saga_id": "saga-1",
    "evento_id": "ev-1",
    "seccion_id": "sec-1",
    "butaca_ids": ["sec-1-A1", "sec-1-A2"],
}


async def test_la_compra_devuelve_la_compra_y_las_entradas(http, stubs):
    """La respuesta junta la saga y las entradas emitidas."""
    respuesta = await http.post("/bff/web/compras", json=CUERPO_COMPRA, headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["compra"]["saga_id"] == "saga-1"
    assert cuerpo["compra"]["estado"] == "CONFIRMADA"
    assert cuerpo["compra"]["cantidad"] == 2
    assert [entrada["entrada_id"] for entrada in cuerpo["entradas"]] == ["ent-1", "ent-2"]
    assert cuerpo["entradas"][0]["qr"] == "TKT-saga-1-sec-1-A1-abc12345"


async def test_el_cliente_id_de_la_compra_es_el_de_la_cabecera(http, stubs):
    """El comprador es el de `X-Client-Id`: el BFF no lo inventa."""
    respuesta = await http.post(
        "/bff/web/compras",
        json=CUERPO_COMPRA,
        headers={"X-Client-Id": "cliente-9"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["compra"]["cliente_id"] == "cliente-9"
    # Y es el cliente_id que llega al dominio, no solo el de la respuesta.
    peticion = stubs["compras"].ultima_llamada["request"]
    assert peticion.cliente_id == "cliente-9"


async def test_las_rutas_de_negocio_exigen_la_identidad_del_cliente(http):
    """Sin `X-Client-Id` la peticion no vino por la puerta: 401."""
    assert (await http.get("/bff/web/catalogo")).status_code == 401
    assert (await http.get("/bff/web/secciones/sec-1/butacas")).status_code == 401
    assert (await http.get("/bff/web/compras/saga-1")).status_code == 401
    assert (
        await http.post("/bff/web/compras", json=CUERPO_COMPRA)
    ).status_code == 401


async def test_la_compra_consultada_compone_compra_reserva_y_entradas(http):
    """La consulta de una saga junta los tres servicios."""
    respuesta = await http.get("/bff/web/compras/saga-1", headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["compra"]["saga_id"] == "saga-1"
    assert cuerpo["compra"]["estado"] == "CONFIRMADA"
    assert cuerpo["reserva"]["reserva_id"] == "res-1"
    assert cuerpo["reserva"]["saga_id"] == "saga-1"
    assert cuerpo["reserva"]["seccion_id"] == "sec-1"
    assert cuerpo["reserva"]["estado"] == "ACTIVA"
    assert cuerpo["reserva"]["precio_centavos"] == 55000
    assert cuerpo["reserva"]["creada_en"] == "2026-03-01T20:04:00"
    assert [butaca["butaca_id"] for butaca in cuerpo["reserva"]["butacas"]] == ["sec-1-A1"]
    assert [entrada["entrada_id"] for entrada in cuerpo["entradas"]] == ["ent-1", "ent-2"]
    assert cuerpo["degradado"] is False


async def test_la_compra_degrada_cuando_emision_no_responde(construir_app, stubs):
    """`emision` es opcional en la compra: la saga vale, las entradas no."""
    stubs["emision"].error = error_grpc()
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.post("/bff/web/compras", json=CUERPO_COMPRA, headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["compra"]["saga_id"] == "saga-1"
    assert cuerpo["entradas"] == []


async def test_la_consulta_de_una_saga_degrada_sin_reserva_ni_entradas(construir_app, stubs):
    """`butacas` y `emision` son opcionales en la consulta: 200 degradado."""
    stubs["butacas"].error = error_grpc()
    stubs["emision"].error = error_grpc()
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get("/bff/web/compras/saga-1", headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["compra"]["saga_id"] == "saga-1"
    assert cuerpo["reserva"] is None
    assert cuerpo["entradas"] == []
    assert cuerpo["degradado"] is True


async def test_la_compra_falla_con_503_cuando_compras_no_responde(construir_app, stubs):
    """`compras` es esencial: si cae, 503 con el codigo de `common.errors`."""
    stubs["compras"].error = error_grpc()
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.post("/bff/web/compras", json=CUERPO_COMPRA, headers=CABECERA)

    assert respuesta.status_code == 503
    assert respuesta.status_code != 500
    assert respuesta.json()["code"] == "DEPENDENCY_UNAVAILABLE"


async def test_el_cuerpo_de_la_compra_se_acepta_sin_tipo_de_contenido(http):
    """El gateway reenvia el cuerpo pero no su `Content-Type`.

    La compra que llega por el gateway llega sin `Content-Type`, asi que el
    BFF no puede exigirlo: lee el cuerpo como JSON sea cual sea su tipo.
    """
    respuesta = await http.post(
        "/bff/web/compras",
        content=json.dumps(CUERPO_COMPRA).encode("utf-8"),
        headers={**CABECERA, "Content-Type": "text/plain"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["compra"]["saga_id"] == "saga-1"


async def test_un_cuerpo_que_no_es_json_es_un_400(http):
    """Un cuerpo roto se responde con 400 `INVALID`, no con un 500."""
    respuesta = await http.post(
        "/bff/web/compras",
        content=b"esto no es json",
        headers={**CABECERA, "Content-Type": "text/plain"},
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["code"] == "INVALID"


async def test_una_compra_sin_butacas_es_un_400(http):
    """No se compra nada sin elegir butacas."""
    respuesta = await http.post(
        "/bff/web/compras",
        json={"saga_id": "saga-1", "evento_id": "ev-1", "seccion_id": "sec-1", "butaca_ids": []},
        headers=CABECERA,
    )

    assert respuesta.status_code == 400
    assert respuesta.json()["code"] == "INVALID"


async def test_el_saga_id_se_genera_cuando_el_cliente_no_manda_uno(http, stubs):
    """Sin `saga_id` el BFF genera uno, igual que hace el gateway."""
    respuesta = await http.post(
        "/bff/web/compras",
        json={"evento_id": "ev-1", "seccion_id": "sec-1", "butaca_ids": ["sec-1-A1"]},
        headers=CABECERA,
    )

    assert respuesta.status_code == 200
    saga_id = respuesta.json()["compra"]["saga_id"]
    assert saga_id
    assert stubs["compras"].ultima_llamada["request"].saga_id == saga_id


async def test_la_moneda_del_cuerpo_es_opcional(http, stubs):
    """Sin `moneda` en el cuerpo manda la del dominio: `COP`."""
    respuesta = await http.post(
        "/bff/web/compras",
        json={
            "saga_id": "saga-1",
            "evento_id": "ev-1",
            "seccion_id": "sec-1",
            "butaca_ids": ["sec-1-A1"],
        },
        headers=CABECERA,
    )

    assert respuesta.status_code == 200
    assert respuesta.json()["compra"]["moneda"] == "COP"
