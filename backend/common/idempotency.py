"""Idempotency store.

A command that changes state carries an idempotency key. The store keeps the
result of the first execution, so a replayed command (a retry, a duplicated
message, a double click) returns the stored result instead of charging or
reserving twice. This is the base of the idempotency requirement of the saga:
every step and every compensation is keyed by `(saga_id, step)`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base

EN_CURSO = "EN_CURSO"
COMPLETADO = "COMPLETADO"


class IdempotencyRecord(Base):
    """One row per logical command key."""

    __tablename__ = "idempotencia"

    clave: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default=EN_CURSO)
    resultado: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    creado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class IdempotencyStore:
    """Read and write access to the idempotency records of one session."""

    def __init__(self, session) -> None:
        self._session = session

    def get(self, key: str) -> dict | None:
        """Return the stored record of a key, or None when it is unknown."""
        record = self._session.get(IdempotencyRecord, key)
        if record is None:
            return None
        return {
            "estado": record.estado,
            "resultado": record.resultado,
            "request_hash": record.request_hash,
        }

    def reserve(self, key: str, request_hash: str) -> bool:
        """Claim a key.

        Returns True when the caller owns the execution of that key and False
        when the key is already known, which means the command is a replay.
        """
        if self._session.get(IdempotencyRecord, key) is not None:
            return False
        self._session.add(
            IdempotencyRecord(clave=key, request_hash=request_hash, estado=EN_CURSO)
        )
        self._session.flush()
        return True

    def save_result(self, key: str, request_hash: str, resultado) -> None:
        """Store the result of an executed command."""
        record = self._session.get(IdempotencyRecord, key)
        if record is None:
            record = IdempotencyRecord(clave=key, request_hash=request_hash, estado=EN_CURSO)
            self._session.add(record)
        record.request_hash = request_hash
        record.estado = COMPLETADO
        record.resultado = resultado
        self._session.flush()


def hash_request(payload) -> str:
    """Stable hash of a request body.

    It detects the dangerous case of a key reused with a different body: the
    key is the same logical command, the payload is not.
    """
    serialised = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialised.encode("utf-8")).hexdigest()
