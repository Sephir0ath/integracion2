"""SQLAlchemy engine, session and schema helpers.

Each service owns its own database, so every service builds its own engine
from `DATABASE_URL` and defines its own models on the shared `Base`.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    """Declarative base shared by every model of the service."""


def create_engine(url: str, **kwargs) -> Engine:
    """Build an engine with connection pre-ping.

    Pre-ping avoids handing a dead connection to a request after a database
    restart. The pool options are skipped for SQLite, whose pool does not
    accept them.
    """
    options: dict = {"pool_pre_ping": True, "future": True}
    if not url.startswith("sqlite"):
        options.update({"pool_size": 5, "max_overflow": 10})
    options.update(kwargs)
    return sa_create_engine(url, **options)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Session context manager: commit on success, roll back on failure."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_schema(engine: Engine, base: type[Base] = Base) -> None:
    """Create the tables of the models registered on `base`."""
    base.metadata.create_all(engine)


def wait_for_database(engine: Engine, attempts: int = 30, delay: float = 1.0) -> None:
    """Block until the database accepts connections.

    Compose starts the application before PostgreSQL finishes its first boot;
    retrying here keeps the container from crash looping.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect():
                return
        except Exception as exc:  # noqa: BLE001 - the error is reported below
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"database not reachable after {attempts} attempts") from last_error
