"""Seed of the demo catalogue.

The system must show a usable catalogue as soon as it starts, without a
manual step. The seed is **idempotent**: it only inserts what is missing
and it never overwrites an existing event or section.

The catalogue is three events with their sections and the seat rows of
every section (eight seats per row, continuous numbering).
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from common.db import session_scope
from common.logging import log_event

from .models import Butaca, Evento, Seccion
from .service import crear_butacas, parsear_fecha

logger = logging.getLogger("butacas.seed")

CATALOGO: list[dict] = [
    {
        "evento_id": "ev_concierto_sinfonico",
        "nombre": "Concierto sinfonico: gala de apertura",
        "lugar": "Teatro Colon, Bogota",
        "fecha": "2026-12-12T20:00:00Z",
        "secciones": [
            {
                "seccion_id": "sec_concierto_platea",
                "nombre": "Platea principal",
                "capacidad_total": 24,
                "precio_centavos": 85000,
            },
            {
                "seccion_id": "sec_concierto_balcon",
                "nombre": "Balcon",
                "capacidad_total": 16,
                "precio_centavos": 42000,
            },
        ],
    },
    {
        "evento_id": "ev_partido_decisivo",
        "nombre": "Partido decisivo: Millonarios vs. Santa Fe",
        "lugar": "Estadio El Campin, Bogota",
        "fecha": "2026-11-28T19:30:00Z",
        "secciones": [
            {
                "seccion_id": "sec_partido_tribuna_norte",
                "nombre": "Tribuna norte",
                "capacidad_total": 32,
                "precio_centavos": 55000,
            },
            {
                "seccion_id": "sec_partido_tribuna_sur",
                "nombre": "Tribuna sur",
                "capacidad_total": 24,
                "precio_centavos": 55000,
            },
        ],
    },
    {
        "evento_id": "ev_obra_bernarda_alba",
        "nombre": "Obra de teatro: La casa de Bernarda Alba",
        "lugar": "Teatro Santiago, Medellin",
        "fecha": "2027-01-16T21:00:00Z",
        "secciones": [
            {
                "seccion_id": "sec_obra_sala_principal",
                "nombre": "Sala principal",
                "capacidad_total": 16,
                "precio_centavos": 38000,
            }
        ],
    },
]


def sembrar_evento(session, evento: dict) -> dict:
    """Insert one event of the catalogue, skipping what already exists.

    A section that already exists is not overwritten and does not receive
    new seat rows.
    """
    creado_evento = False
    if session.get(Evento, evento["evento_id"]) is None:
        session.add(
            Evento(
                evento_id=evento["evento_id"],
                nombre=evento["nombre"],
                lugar=evento["lugar"],
                fecha=parsear_fecha(evento["fecha"]),
            )
        )
        session.flush()
        creado_evento = True

    secciones_creadas = 0
    butacas_creadas = 0
    for seccion in evento["secciones"]:
        if session.get(Seccion, seccion["seccion_id"]) is not None:
            # La seccion ya existe: no se sobrescribe ni se le agregan
            # butacas.
            continue
        session.add(
            Seccion(
                seccion_id=seccion["seccion_id"],
                evento_id=evento["evento_id"],
                nombre=seccion["nombre"],
                capacidad_total=int(seccion["capacidad_total"]),
                precio_centavos=int(seccion["precio_centavos"]),
            )
        )
        session.flush()
        secciones_creadas += 1
        for butaca in crear_butacas(seccion["seccion_id"], int(seccion["capacidad_total"])):
            session.add(Butaca(**butaca))
            butacas_creadas += 1

    return {
        "evento_creado": creado_evento,
        "secciones_creadas": secciones_creadas,
        "butacas_creadas": butacas_creadas,
    }


def sembrar_si_esta_vacio(engine) -> dict:
    """Seed the catalogue, but only when the inventory is empty.

    The seed never overwrites: when the inventory already has rows the
    catalogue is left alone.
    """
    with session_scope(engine) as session:
        eventos = session.execute(select(func.count()).select_from(Evento)).scalar_one()
        secciones = session.execute(select(func.count()).select_from(Seccion)).scalar_one()
        butacas = session.execute(select(func.count()).select_from(Butaca)).scalar_one()
        if eventos or secciones or butacas:
            log_event(
                logger,
                "butacas_catalogo_existente",
                eventos=int(eventos),
                secciones=int(secciones),
                butacas=int(butacas),
            )
            return {
                "sembrado": False,
                "eventos": int(eventos),
                "secciones": int(secciones),
                "butacas": int(butacas),
            }

        eventos_creados = 0
        secciones_creadas = 0
        butacas_creadas = 0
        for evento in CATALOGO:
            resultado = sembrar_evento(session, evento)
            eventos_creados += 1 if resultado["evento_creado"] else 0
            secciones_creadas += resultado["secciones_creadas"]
            butacas_creadas += resultado["butacas_creadas"]

    log_event(
        logger,
        "butacas_catalogo_sembrado",
        eventos=eventos_creados,
        secciones=secciones_creadas,
        butacas=butacas_creadas,
    )
    return {
        "sembrado": True,
        "eventos": eventos_creados,
        "secciones": secciones_creadas,
        "butacas": butacas_creadas,
    }
