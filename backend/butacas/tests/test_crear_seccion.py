"""Tests of the creation of a section and of its seating plan.

The seats of a section are rows of the table `butaca` that point to the row
of the table `seccion`, and the database refuses a seat whose section does
not exist. So the section has to be written **before** its seats.

These tests run over a base that enforces the foreign keys (`servicio_con_fk`),
because SQLite does not enforce them by default while PostgreSQL does: without
that, the suite would happily accept the rows that the real base rejects.
"""

from __future__ import annotations

from ..models import LIBRE


def test_crear_seccion_persiste_la_seccion_y_sus_butacas(servicio_con_fk):
    evento = servicio_con_fk.crear_evento(
        "Concierto de prueba", "Teatro de prueba", "2026-12-01T20:00:00Z"
    )

    seccion = servicio_con_fk.crear_seccion(evento["evento_id"], "Platea", 10, 25000)

    assert seccion["seccion_id"]
    assert seccion["evento_id"] == evento["evento_id"]
    assert seccion["nombre"] == "Platea"
    assert seccion["capacidad_total"] == 10
    assert seccion["precio_centavos"] == 25000
    # El contador sale de las filas que existen de verdad, no de un numero
    # guardado: si las butacas no se hubieran escrito, daria 0.
    assert seccion["cantidad_disponible"] == 10


def test_las_butacas_de_una_seccion_nueva_apuntan_a_esa_seccion(servicio_con_fk):
    evento = servicio_con_fk.crear_evento(
        "Concierto de prueba", "Teatro de prueba", "2026-12-01T20:00:00Z"
    )
    seccion = servicio_con_fk.crear_seccion(evento["evento_id"], "Platea", 10, 25000)

    butacas = servicio_con_fk.listar_butacas(seccion["seccion_id"])

    assert len(butacas) == 10
    assert all(b["seccion_id"] == seccion["seccion_id"] for b in butacas)
    assert all(b["estado"] == LIBRE for b in butacas)
    assert all(b["butaca_id"].startswith(seccion["seccion_id"] + "-") for b in butacas)


def test_una_seccion_nueva_no_afecta_a_las_butacas_de_las_otras(servicio_con_fk):
    evento = servicio_con_fk.crear_evento(
        "Concierto de prueba", "Teatro de prueba", "2026-12-01T20:00:00Z"
    )
    primera = servicio_con_fk.crear_seccion(evento["evento_id"], "Platea", 8, 25000)
    segunda = servicio_con_fk.crear_seccion(evento["evento_id"], "Balcon", 4, 15000)

    butacas_primera = servicio_con_fk.listar_butacas(primera["seccion_id"])
    butacas_segunda = servicio_con_fk.listar_butacas(segunda["seccion_id"])

    assert len(butacas_primera) == 8
    assert len(butacas_segunda) == 4
    assert not {b["butaca_id"] for b in butacas_primera} & {b["butaca_id"] for b in butacas_segunda}
