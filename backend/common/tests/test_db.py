"""Tests of the database helpers."""

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from common.db import Base, create_engine, init_schema, session_scope, wait_for_database
from common.idempotency import IdempotencyRecord


@pytest.fixture()
def engine():
    return create_engine("sqlite://")


def test_init_schema_creates_the_tables(engine):
    init_schema(engine, Base)
    assert "idempotencia" in inspect(engine).get_table_names()


def test_session_scope_commits_the_work(engine):
    init_schema(engine, Base)

    with session_scope(engine) as session:
        session.add(IdempotencyRecord(clave="k1", request_hash="h", estado="EN_CURSO"))

    with Session(engine) as session:
        assert session.get(IdempotencyRecord, "k1") is not None


def test_session_scope_rolls_back_on_error(engine):
    init_schema(engine, Base)

    with pytest.raises(RuntimeError):
        with session_scope(engine) as session:
            session.add(IdempotencyRecord(clave="k2", request_hash="h", estado="EN_CURSO"))
            raise RuntimeError("boom")

    with Session(engine) as session:
        assert session.get(IdempotencyRecord, "k2") is None


def test_wait_for_database_raises_after_the_attempts():
    engine = create_engine("postgresql+psycopg2://app:app@127.0.0.1:1/inexistente")
    with pytest.raises(RuntimeError):
        wait_for_database(engine, attempts=2, delay=0.01)


def test_wait_for_database_returns_when_the_connection_succeeds(engine):
    init_schema(engine, Base)
    wait_for_database(engine, attempts=1, delay=0.01)
