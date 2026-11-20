"""Tests of the deletion of an event and of its seating plan.

Deleting an event means deleting its sections and their seats first: the
seats point to the section and the section points to the event, and the
database refuses to delete a row that another one still references.

These tests run over a base that enforces the foreign keys
(`servicio_con_fk`): without that, SQLite accepts the deletes and the
suite would not notice that the order of the statements was wrong.
"""

from __future__ import annotations

import pytest

from common.errors import CODES, ServiceError


def test_eliminar_un_evento_borra_sus_secciones_y_sus_butacas(servicio_con_fk):
    evento = servicio_con_fk.crear_evento(
        "Concierto de prueba", "Teatro de prueba", "2026-12-01T20:00:00Z"
    )
    seccion = servicio_con_fk.crear_seccion(evento["evento_id"], "Platea", 8, 25000)

    resultado = servicio_con_fk.eliminar_evento(evento["evento_id"])

    assert resultado["eliminado"] is True
    # No queda ni el evento, ni su seccion, ni sus butacas.
    assert servicio_con_fk.listar_eventos() == []
    assert servicio_con_fk.listar_secciones(evento["evento_id"]) == []
    with pytest.raises(ServiceError) as error:
        servicio_con_fk.listar_butacas(seccion["seccion_id"])
    assert error.value.code == CODES.NOT_FOUND


def test_no_se_puede_eliminar_un_evento_con_butacas_vendidas(servicio_con_fk):
    evento = servicio_con_fk.crear_evento(
        "Concierto de prueba", "Teatro de prueba", "2026-12-01T20:00:00Z"
    )
    seccion = servicio_con_fk.crear_seccion(evento["evento_id"], "Platea", 8, 25000)
    butacas = servicio_con_fk.listar_butacas(seccion["seccion_id"])
    elegidas = [butacas[0]["butaca_id"]]
    servicio_con_fk.reservar("saga-1", evento["evento_id"], seccion["seccion_id"], elegidas)
    servicio_con_fk.confirmar_venta("saga-1")

    with pytest.raises(ServiceError) as error:
        servicio_con_fk.eliminar_evento(evento["evento_id"])

    assert error.value.code == CODES.INVALID
    # Y el evento sigue ahi: el rechazo no dejo nada a medias.
    assert [e["evento_id"] for e in servicio_con_fk.listar_eventos()] == [evento["evento_id"]]
