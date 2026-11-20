"""Ticket model of the `emision` service.

One table, `entrada`, with one row per issued ticket. The
`idempotencia` table is **not** defined here: it belongs to the shared
library (`common.idempotency`) and every service of the system uses the
same one. It is imported here so `init_schema(engine, Base)` creates it
too.

The unique constraint `(saga_id, butaca_id)` is the protection of the
seat inside the saga: the same seat can never have two tickets of the
same saga, so a replayed `EMITIR` cannot issue a second ticket for a
seat that already has one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base
from common.idempotency import IdempotencyRecord  # noqa: F401  (registers the shared table)

# States of a ticket.
EMITIDA = "EMITIDA"
ANULADA = "ANULADA"

ESTADOS_ENTRADA = (EMITIDA, ANULADA)


def ahora() -> datetime:
    """Current time in UTC, used as the default of the timestamp columns."""
    return datetime.now(timezone.utc)


def a_iso(valor: datetime | None) -> str:
    """Render a timestamp as ISO-8601 (UTC)."""
    if valor is None:
        return ""
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.isoformat()


class Entrada(Base):
    """One issued ticket."""

    __tablename__ = "entrada"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    saga_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    butaca_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seccion_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    fila: Mapped[str] = mapped_column(String(4), nullable=False, default="")
    numero: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Payload of the QR of the ticket: deterministic and verifiable.
    qr: Mapped[str] = mapped_column(String(255), nullable=False)
    estado: Mapped[str] = mapped_column(String(16), nullable=False, default=EMITIDA, index=True)
    emitida_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    anulada_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("saga_id", "butaca_id", name="uq_entrada_saga_butaca"),
    )

    def como_datos(self) -> dict:
        """The ticket as a plain dict, the shape stored in the idempotency store."""
        return {
            "entrada_id": self.id,
            "saga_id": self.saga_id,
            "butaca_id": self.butaca_id,
            "seccion_id": self.seccion_id or "",
            "fila": self.fila or "",
            "numero": int(self.numero or 0),
            "qr": self.qr,
            "estado": self.estado,
            "emitida_en": a_iso(self.emitida_en),
            "anulada_en": a_iso(self.anulada_en),
        }
