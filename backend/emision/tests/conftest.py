"""Fixtures of the tests of the `emision` service.

The tests run without PostgreSQL and without the `pagos` service running:
they use a **temporary SQLite file** (and not `:memory:`, because the
tests open more than one session on the same database) and the real
sessions of `common.db`.

The verification of the payment uses the fake stub of `tests/fakes.py`,
injected where the client of the dependency is built.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from common.config import Settings
from common.db import create_engine, init_schema, session_scope

from ..models import Base  # noqa: F401  (registers the tables)
from ..pagos_client import PagosClient
from ..service import EmisionService
from .fakes import FakePagosStub


@pytest.fixture()
def engine(tmp_path):
    """Engine over a temporary SQLite file."""
    url = f"sqlite:///{tmp_path / 'emision.db'}"
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
def pagos_stub(engine, conexiones_en_uso):
    """Fake of the `ConsultarPago` method of the `pagos` service."""
    return FakePagosStub(probe=lambda: conexiones_en_uso.append(engine.pool.checkedout()))


@pytest.fixture()
def conexiones_en_uso():
    """Number of connections of the pool while the dependency was called."""
    return []


@pytest.fixture()
def cliente_pagos(pagos_stub):
    """Client of the `pagos` service, with the fake stub injected."""
    return PagosClient(Settings.from_env(), stub=pagos_stub)


@pytest.fixture()
def servicio(sesiones, cliente_pagos):
    """Domain service of the `emision` service."""
    return EmisionService(sesiones, cliente_pagos)


@pytest.fixture()
def butacas():
    """Two seats of a section."""
    return [
        {"butaca_id": "sec-1-A1", "seccion_id": "sec-1", "fila": "A", "numero": 1},
        {"butaca_id": "sec-1-A2", "seccion_id": "sec-1", "fila": "A", "numero": 2},
    ]
