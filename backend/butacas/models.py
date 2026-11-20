"""Inventory model of the `butacas` service.

Four tables: the events, their sections, the seat rows and the
reservations. The `idempotencia` table is **not** defined here: it belongs
to the shared library (`common.idempotency`) and every service of the
system uses the same one. It is imported here so `init_schema(engine, Base)`
creates it too.

Every seat is one row, individually identified: that is what makes it
possible to claim a seat exactly once (`RESERVAR`) and to know who owns it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base
from common.idempotency import IdempotencyRecord  # noqa: F401  (registers the shared table)

# States of a seat.
LIBRE = "LIBRE"
RESERVADA = "RESERVADA"
VENDIDA = "VENDIDA"

ESTADOS_BUTACA = (LIBRE, RESERVADA, VENDIDA)

# States of a reservation.
ACTIVA = "ACTIVA"
LIBERADA = "LIBERADA"

ESTADOS_RESERVA = (ACTIVA, LIBERADA, VENDIDA)

# Seats per row of the seating plan.
BUTACAS_POR_FILA = 8


def ahora() -> datetime:
    """Current time in UTC, used as the default of the timestamp columns."""
    return datetime.now(timezone.utc)


class Evento(Base):
    """One event of the venue."""

    __tablename__ = "evento"

    evento_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    lugar: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class Seccion(Base):
    """One section of an event.

    The price is owned by the domain: the client never sends it, it only
    receives it.
    """

    __tablename__ = "seccion"

    seccion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evento_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("evento.evento_id"), nullable=False, index=True
    )
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    capacidad_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    precio_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class Butaca(Base):
    """One seat of a section.

    `estado` and `saga_id` say who owns the seat: a free seat has no owner,
    a reserved or sold seat belongs to the saga that claimed it.

    The unique constraints protect two things: there cannot be two rows for
    the same position of a section, and the same saga cannot claim the same
    position twice.
    """

    __tablename__ = "butaca"

    butaca_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seccion_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("seccion.seccion_id"), nullable=False, index=True
    )
    fila: Mapped[str] = mapped_column(String(4), nullable=False)
    numero: Mapped[int] = mapped_column(Integer, nullable=False)
    estado: Mapped[str] = mapped_column(String(16), nullable=False, default=LIBRE, index=True)
    saga_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint("seccion_id", "fila", "numero", name="uq_butaca_posicion"),
        UniqueConstraint(
            "seccion_id", "saga_id", "fila", "numero", name="uq_butaca_saga_posicion"
        ),
    )


class Reserva(Base):
    """The reservation of a saga over a set of seats.

    A saga reserves once, so the identifier is derived from the saga and the
    saga is unique: the row can be found again without a search.
    """

    __tablename__ = "reserva"

    reserva_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    saga_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    seccion_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    estado: Mapped[str] = mapped_column(String(16), nullable=False, default=ACTIVA)
    precio_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
