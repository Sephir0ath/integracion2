"""Pruebas del catalogo: la composicion de Butacas + Pagos.

El catalogo es la respuesta agregada por el cliente: en un solo cuerpo
viajan los eventos y las secciones (de `butacas`) y los metodos de pago
(de `pagos`).

Lo que distingue al BFF de un proxy esta en la **degradacion elegante**:
`butacas` es esencial (si cae, 503) y `pagos` es opcional (si cae, la
respuesta sigue siendo 200, sin metodos de pago y marcada como degradada).
"""

from __future__ import annotations

from .conftest import cliente_para
from .fakes import error_grpc

CABECERA = {"X-Client-Id": "cliente-1"}


async def test_el_catalogo_compone_eventos_secciones_y_metodos_de_pago(http):
    """El catalogo junta los datos de butacas y de pagos en un solo cuerpo."""
    respuesta = await http.get("/bff/web/catalogo", headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert [evento["evento_id"] for evento in cuerpo["eventos"]] == ["ev-1", "ev-2"]
    assert cuerpo["eventos"][0]["nombre"] == "Partido decisivo"
    assert cuerpo["eventos"][0]["lugar"] == "El Campin"
    assert cuerpo["eventos"][0]["fecha"] == "2026-03-01T20:00:00"
    assert [seccion["seccion_id"] for seccion in cuerpo["secciones"]] == ["sec-1", "sec-2"]
    assert cuerpo["secciones"][0]["precio_centavos"] == 55000
    assert cuerpo["secciones"][0]["capacidad_total"] == 32
    assert cuerpo["secciones"][0]["cantidad_disponible"] == 30
    assert cuerpo["metodos_pago"] == [
        {"metodo": "TARJETA", "descripcion": "Tarjeta de credito o debito"},
        {"metodo": "PSE", "descripcion": "Pago en linea con el banco"},
    ]
    assert cuerpo["pagos_disponible"] is True
    assert cuerpo["degradado"] is False


async def test_el_catalogo_degrada_cuando_pagos_no_responde(construir_app, stubs):
    """`pagos` es opcional: si cae, el catalogo responde 200 sin metodos."""
    stubs["pagos"].error = error_grpc()
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get("/bff/web/catalogo", headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["pagos_disponible"] is False
    assert cuerpo["degradado"] is True
    assert "metodos_pago" not in cuerpo
    # Los datos de la dependencia esencial siguen intactos.
    assert [evento["evento_id"] for evento in cuerpo["eventos"]] == ["ev-1", "ev-2"]
    assert [seccion["seccion_id"] for seccion in cuerpo["secciones"]] == ["sec-1", "sec-2"]


async def test_el_catalogo_devuelve_503_cuando_butacas_no_responde(construir_app, stubs):
    """`butacas` es esencial: si cae, 503 con el codigo de `common.errors`."""
    stubs["butacas"].error = error_grpc()
    app = construir_app()

    async with cliente_para(app) as http:
        respuesta = await http.get("/bff/web/catalogo", headers=CABECERA)

    assert respuesta.status_code == 503
    assert respuesta.status_code != 500
    cuerpo = respuesta.json()
    assert cuerpo["code"] == "DEPENDENCY_UNAVAILABLE"
    assert cuerpo["message"]
