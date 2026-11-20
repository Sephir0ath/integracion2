"""Pruebas del proxy al BFF.

El BFF es la agregacion por cliente: el gateway no hace agregaciones, solo
le pasa la peticion y devuelve **el mismo estado y el mismo cuerpo** que el
BFF respondio.
"""

from __future__ import annotations

import json

import httpx

from ..clients import ClienteBff
from .conftest import cliente_para


async def test_el_gateway_devuelve_el_mismo_estado_y_cuerpo_del_bff(construir_app, bff_registro, tokens):
    """La respuesta del BFF viaja intacta: mismo estado, mismo cuerpo."""
    cuerpo = b'{"eventos":[{"evento_id":"ev-1"}],"degradado":false}'
    bff_registro["respuesta"] = (200, cuerpo, "application/json")
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get(
            "/bff/web/catalogo",
            headers={"Authorization": f"Bearer {tokens.cliente()}"},
        )

    assert respuesta.status_code == 200
    assert respuesta.content == cuerpo
    assert respuesta.json() == {"eventos": [{"evento_id": "ev-1"}], "degradado": False}


async def test_el_gateway_conserva_el_estato_de_error_del_bff(construir_app, bff_registro, tokens):
    """Si el BFF responde 404, el gateway responde 404."""
    bff_registro["respuesta"] = (404, b'{"code":"NOT_FOUND"}', "application/json")
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get(
            "/bff/web/compras/saga-999",
            headers={"Authorization": f"Bearer {tokens.cliente()}"},
        )

    assert respuesta.status_code == 404
    assert respuesta.content == b'{"code":"NOT_FOUND"}'


async def test_el_gateway_manda_la_peticion_al_bff(construir_app, bff_registro, tokens):
    """La ruta, la consulta y el cuerpo llegan al BFF, con la identidad."""
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.post(
            "/bff/web/compras",
            json={"saga_id": "saga-1", "evento_id": "ev-1", "seccion_id": "sec-1", "cantidad": 2},
            headers={"Authorization": f"Bearer {tokens.cliente()}"},
        )

    assert respuesta.status_code == 200
    peticion = bff_registro["peticiones"][-1]
    assert str(peticion.url) == "http://bff:8081/bff/web/compras"
    assert peticion.method == "POST"
    assert json.loads(peticion.content) == {
        "saga_id": "saga-1",
        "evento_id": "ev-1",
        "seccion_id": "sec-1",
        "cantidad": 2,
    }
    assert peticion.headers["x-client-id"] == "cliente-1"
    assert peticion.headers["x-role"] == "cliente"


async def test_la_consulta_del_bff_se_reenvia(construir_app, bff_registro, tokens):
    """Los parametros de la consulta tambien viajan."""
    app = construir_app()

    async with cliente_para(app) as http:
        await http.get(
            "/bff/web/secciones/sec-1/butacas",
            params={"formato": "plano"},
            headers={"Authorization": f"Bearer {tokens.cliente()}"},
        )

    peticion = bff_registro["peticiones"][-1]
    assert str(peticion.url) == "http://bff:8081/bff/web/secciones/sec-1/butacas?formato=plano"


async def test_la_ruta_del_bff_exige_token(construir_app):
    """Las rutas del BFF son de negocio: tambien exigen token."""
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get("/bff/web/catalogo")

    assert respuesta.status_code == 401


async def test_bff_caido_devuelve_503(construir_app, settings, tokens):
    """Si el BFF no responde, el gateway lo dice con 503 y no con 500."""

    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("el bff no responde", request=peticion)

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    app = construir_app(cliente_bff=ClienteBff(settings, cliente=cliente))

    try:
        async with cliente_para(app) as http:
            respuesta = await http.get(
                "/bff/web/catalogo",
                headers={"Authorization": f"Bearer {tokens.cliente()}"},
            )
    finally:
        await cliente.aclose()

    assert respuesta.status_code == 503
    assert respuesta.json()["code"] == "DEPENDENCY_UNAVAILABLE"
