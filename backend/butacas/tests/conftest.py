"""Fixtures of the tests of the `butacas` service.

The tests run without PostgreSQL and without any other service of the
system: they use a **temporary SQLite file** (and not `:memory:`, because
the atomicity test needs two independent sessions on the same file) and the
real sessions of `common.db`.

SQLite supports `UPDATE ... RETURNING`, so the atomic statement of the
reservation works there exactly as it works on PostgreSQL.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from common.db import create_engine, init_schema, session_scope

from ..models import Base, Butaca, Evento, Seccion  # noqa: F401  (registers the tables)
from ..service import ButacasService


@pytest.fixture()
def engine(tmp_path):
    """Engine over a temporary SQLite file."""
    url = f"sqlite:///{tmp_path / 'butacas.db'}"
    motor = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(motor, "connect")
    def _configurar(dbapi_connection, connection_record):  # pragma: no cover - event listener
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    init_schema(motor, Base)
    yield motor
    motor.dispose()


@pytest.fixture()
def sesiones(engine):
    """Provider of sessions, like `common.db.session_scope`."""
    return lambda: session_scope(engine)


@pytest.fixture()
def motor_con_fk(tmp_path):
    """Engine over a temporary SQLite file **with the foreign keys enforced**.

    SQLite does not enforce them by default, and that is exactly why the
    suite never noticed that the seats of a new section were written before
    the section they point to: the database accepted rows that PostgreSQL
    rejects with a `ForeignKeyViolation`.
    """
    url = f"sqlite:///{tmp_path / 'butacas_fk.db'}"
    motor = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(motor, "connect")
    def _forzar_fk(dbapi_connection, connection_record):  # pragma: no cover - event listener
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    init_schema(motor, Base)
    yield motor
    motor.dispose()


@pytest.fixture()
def servicio_con_fk(motor_con_fk):
    """Domain service over a base that enforces the foreign keys."""
    return ButacasService(lambda: session_scope(motor_con_fk))


@pytest.fixture()
def servicio(sesiones):
    """Domain service of the `butacas` service."""
    return ButacasService(sesiones)


@pytest.fixture()
def catalogo(servicio):
    """One event with one section of 16 seats."""
    evento = servicio.crear_evento(
        "Concierto de prueba", "Teatro de prueba", "2026-12-01T20:00:00Z"
    )
    seccion = servicio.crear_seccion(evento["evento_id"], "Platea", 16, 25000)
    return evento, seccion


@pytest.fixture()
def butacas(servicio, catalogo):
    """Map of the seats of the section of the catalogue, by identifier."""
    _evento, seccion = catalogo
    return {b["butaca_id"]: b for b in servicio.listar_butacas(seccion["seccion_id"])}
