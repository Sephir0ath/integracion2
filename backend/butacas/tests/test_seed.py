"""Tests of the seed of the catalogue.

The system must show a usable catalogue as soon as it starts, without a
manual step, and the seed must never overwrite what already exists.
"""

from __future__ import annotations

from sqlalchemy import func, select

from common.db import session_scope

from ..models import Butaca, Evento, Seccion
from ..seed import CATALOGO, sembrar_evento, sembrar_si_esta_vacio


def _conteo(engine, modelo) -> int:
    with engine.connect() as conexion:
        return int(
            conexion.execute(select(func.count()).select_from(modelo)).scalar_one()
        )


def test_la_siembra_crea_el_catalogo_con_sus_butacas(engine):
    resultado = sembrar_si_esta_vacio(engine)

    assert resultado["sembrado"] is True
    assert resultado["eventos"] == 3
    assert resultado["secciones"] == 5
    assert _conteo(engine, Evento) == 3
    assert _conteo(engine, Seccion) == 5
    assert _conteo(engine, Butaca) == sum(
        seccion["capacidad_total"] for evento in CATALOGO for seccion in evento["secciones"]
    )


def test_la_siembra_es_idempotente(engine):
    sembrar_si_esta_vacio(engine)
    segunda = sembrar_si_esta_vacio(engine)

    assert segunda["sembrado"] is False
    assert _conteo(engine, Evento) == 3
    assert _conteo(engine, Seccion) == 5
    assert _conteo(engine, Butaca) == 112


def test_la_siembra_no_toca_un_catalogo_existente(engine, servicio):
    servicio.crear_evento("Mi evento", "Mi sala", "2027-05-05T20:00:00Z")

    resultado = sembrar_si_esta_vacio(engine)

    assert resultado["sembrado"] is False
    with engine.connect() as conexion:
        nombres = conexion.execute(select(Evento.nombre)).scalars().all()
    assert nombres == ["Mi evento"]


def test_una_seccion_existente_no_se_sobrescribe(engine, servicio):
    """Una seccion sembrada no pisa una seccion que ya existe."""
    seccion_id = CATALOGO[0]["secciones"][0]["seccion_id"]
    evento = servicio.crear_evento("Mi evento", "Mi sala", "2027-05-05T20:00:00Z")
    with session_scope(engine) as session:
        session.add(
            Seccion(
                seccion_id=seccion_id,
                evento_id=evento["evento_id"],
                nombre="Mi seccion",
                capacidad_total=4,
                precio_centavos=999,
            )
        )

    with session_scope(engine) as session:
        resultado = sembrar_evento(session, CATALOGO[0])

    assert resultado["secciones_creadas"] == 1, "solo se crea la seccion que falta"
    with engine.connect() as conexion:
        precio = conexion.execute(
            select(Seccion.precio_centavos).where(Seccion.seccion_id == seccion_id)
        ).scalar_one()
        butacas = conexion.execute(
            select(Butaca.butaca_id).where(Butaca.seccion_id == seccion_id)
        ).all()

    assert precio == 999, "la seccion existente no se sobrescribe"
    assert butacas == [], "no se crean butacas de una seccion que ya existia"
