"""Fixtures of the tests of the `compras` service.

The tests run without PostgreSQL and without the three domain services
running: they use a **temporary SQLite file** (and not `:memory:`, because
the tests open more than one session on the same database) and the real
sessions of `common.db`.

The dependencies are the fakes of `tests/fakes.py`, injected where the
service is built.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event

from common.config import Settings
from common.db import create_engine, init_schema, session_scope

from ..models import Base  # noqa: F401  (registers the tables)
from ..service import ComprasService
from .fakes import FakeButacas, FakeEmision, FakePagos, LlamadasFake


@pytest.fixture()
def engine(tmp_path):
    """Engine over a temporary SQLite file."""
    url = f"sqlite:///{tmp_path / 'compras.db'}"
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
def llamadas():
    """Shared registry of the calls made to the dependencies."""
    return LlamadasFake()


@pytest.fixture()
def butacas(llamadas):
    """Fake of the `butacas` service."""
    return FakeButacas(llamadas)


@pytest.fixture()
def pagos(llamadas):
    """Fake of the `pagos` service."""
    return FakePagos(llamadas)


@pytest.fixture()
def emision(llamadas):
    """Fake of the `emision` service."""
    return FakeEmision(llamadas)


@pytest.fixture()
def servicio(sesiones, butacas, pagos, emision):
    """Orchestrator of the saga, with the fakes of the dependencies."""
    return ComprasService(sesiones, butacas, pagos, emision, deadline=0.5)


@pytest.fixture()
def settings():
    """Settings of the service, for the parts that read them."""
    return Settings.from_env()
