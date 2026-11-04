"""Tests of the idempotency store: the base of the idempotent saga steps."""

import pytest
from sqlalchemy.orm import Session

from common.db import Base, create_engine, init_schema
from common.idempotency import COMPLETADO, IdempotencyRecord, IdempotencyStore, hash_request


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    init_schema(engine, Base)
    with Session(engine) as db_session:
        yield db_session


def test_reserve_claims_the_key_only_once(session):
    store = IdempotencyStore(session)
    assert store.reserve("saga-1:RESERVAR", "hash-a") is True
    assert store.reserve("saga-1:RESERVAR", "hash-a") is False


def test_reserve_accepts_different_keys(session):
    store = IdempotencyStore(session)
    assert store.reserve("saga-1:RESERVAR", "hash-a") is True
    assert store.reserve("saga-1:COBRAR", "hash-b") is True


def test_unknown_key_returns_none(session):
    store = IdempotencyStore(session)
    assert store.get("no-existe") is None


def test_saved_result_is_returned_by_get(session):
    store = IdempotencyStore(session)
    store.reserve("saga-1:COBRAR", "hash-a")
    store.save_result("saga-1:COBRAR", "hash-a", {"pago_id": "p-1", "estado": "CONFIRMADO"})

    record = store.get("saga-1:COBRAR")
    assert record is not None
    assert record["estado"] == COMPLETADO
    assert record["resultado"]["pago_id"] == "p-1"


def test_save_result_without_reserve_creates_the_record(session):
    store = IdempotencyStore(session)
    store.save_result("saga-2:COBRAR", "hash-c", {"estado": "CONFIRMADO"})

    record = store.get("saga-2:COBRAR")
    assert record is not None
    assert record["resultado"]["estado"] == "CONFIRMADO"


def test_hash_request_is_stable_and_changes_with_the_payload():
    assert hash_request({"a": 1, "b": 2}) == hash_request({"b": 2, "a": 1})
    assert hash_request({"a": 1}) != hash_request({"a": 2})


def test_record_table_is_named_idempotencia(session):
    assert IdempotencyRecord.__tablename__ == "idempotencia"
