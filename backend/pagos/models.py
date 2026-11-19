"""Ledger model of the `pagos` service.

The `idempotencia` table is **not** defined here: it belongs to the shared
library (`common.idempotency`) and every service of the system uses the same
one. It is imported here so `init_schema(engine, Base)` creates it too.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base
from common.idempotency import IdempotencyRecord  # noqa: F401  (registers the shared table)

CONFIRMADO = "CONFIRMADO"
REEMBOLSADO = "REEMBOLSADO"
FALLIDO = "FALLIDO"

ESTADOS = (CONFIRMADO, REEMBOLSADO, FALLIDO)

MONEDA_POR_DEFECTO = "COP"


def ahora() -> datetime:
    """Current time in UTC, used as the default of the timestamp columns."""
    return datetime.now(timezone.utc)


class Pago(Base):
    """One payment of the ledger."""

    __tablename__ = "pagos"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    saga_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    monto_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moneda: Mapped[str] = mapped_column(String(8), nullable=False, default=MONEDA_POR_DEFECTO)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, default=CONFIRMADO)
    # Identifier of the payment at the gateway: it is what the refund uses.
    pasarela_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Human readable reference of the gateway (shown to the client).
    pasarela_referencia: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizado_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=ahora, onupdate=ahora
    )

    def como_datos(self) -> dict:
        """The payment as a plain dict, the shape stored in the idempotency store."""
        return {
            "pago_id": self.id,
            "saga_id": self.saga_id,
            "monto_centavos": int(self.monto_centavos),
            "moneda": self.moneda,
            "estado": self.estado,
            "pasarela_ref": self.pasarela_ref or "",
            "pasarela_referencia": self.pasarela_referencia or "",
            "idempotency_key": self.idempotency_key or "",
            "creado_en": self.creado_en.isoformat() if self.creado_en else "",
            "actualizado_en": self.actualizado_en.isoformat() if self.actualizado_en else "",
        }
