"""Pruebas del plano de la sala.

El plano viene de un solo servicio (`butacas`), pero es la forma que el
cliente web entiende: la seccion pedida y sus butacas con el estado de
cada una.
"""

from __future__ import annotations

CABECERA = {"X-Client-Id": "cliente-1"}


async def test_el_plano_de_la_sala_devuelve_las_butacas(http):
    """La respuesta trae la seccion pedida y sus butacas."""
    respuesta = await http.get("/bff/web/secciones/sec-1/butacas", headers=CABECERA)

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["seccion_id"] == "sec-1"
    assert cuerpo["butacas"] == [
        {
            "butaca_id": "sec-1-A1",
            "seccion_id": "sec-1",
            "fila": "A",
            "numero": 1,
            "estado": "LIBRE",
            "saga_id": "",
        },
        {
            "butaca_id": "sec-1-A2",
            "seccion_id": "sec-1",
            "fila": "A",
            "numero": 2,
            "estado": "RESERVADA",
            "saga_id": "saga-1",
        },
    ]
