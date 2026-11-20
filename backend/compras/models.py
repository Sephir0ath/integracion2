"""Estado persistido de la saga de compra.

El orquestador es el unico que conoce la saga completa, asi que es el unico
que guarda su estado: una fila por saga (`saga`), una fila por paso
(`paso_saga`) y una fila por compensacion (`compensacion_saga`). El estado
persistido es la fuente de verdad de `ConsultarCompra` y de la respuesta
idempotente de `IniciarCompra`: un proceso que se reinicia a mitad de una
saga no pierde el rastro de lo que ya ocurrio.

La tabla `idempotencia` **no** se define aqui: pertenece a la biblioteca
comun (`common.idempotency`) y todos los servicios del sistema comparten la
misma. Se importa para que `init_schema(engine, Base)` la cree tambien, que
es lo que hace `emision/models.py`.

El plan de compensacion no se guarda en una columna: se deriva de los pasos
que quedaron `OK` y se aplica en orden inverso, que es la regla de la saga
(S3.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from common.db import Base
from common.idempotency import IdempotencyRecord  # noqa: F401  (registra la tabla compartida)

# Estados por los que pasa la saga.
PENDIENTE = "PENDIENTE"
RUNNING = "RUNNING"
CONFIRMADA = "CONFIRMADA"
COMPENSADA = "COMPENSADA"
FALLIDA = "FALLIDA"

ESTADOS_SAGA = (PENDIENTE, RUNNING, CONFIRMADA, COMPENSADA, FALLIDA)

# Los pasos, en el orden en que la saga los corre.
PASOS = ("RESERVAR", "COBRAR", "EMITIR")

# Resultado de un paso o de una compensacion.
OK = "OK"
ERROR = "ERROR"
SKIPPED = "SKIPPED"

# La compensacion de cada paso. Se aplican en orden inverso al de los pasos.
COMPENSACION_DE_PASO = {
    "RESERVAR": "LIBERAR",
    "COBRAR": "REEMBOLSAR",
    "EMITIR": "ANULAR",
}

COMPENSACIONES = tuple(COMPENSACION_DE_PASO[paso] for paso in PASOS)

# Moneda por defecto del sistema.
MONEDA_POR_DEFECTO = "COP"


def ahora() -> datetime:
    """Momento actual en UTC, valor por defecto de las columnas de tiempo."""
    return datetime.now(timezone.utc)


def a_iso(valor: datetime | None) -> str:
    """Render a timestamp as ISO-8601 (UTC)."""
    if valor is None:
        return ""
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.isoformat()


class Saga(Base):
    """Una compra orquestada: el estado de la saga y la peticion que la creo."""

    __tablename__ = "saga"

    saga_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    estado: Mapped[str] = mapped_column(String(16), nullable=False, default=PENDIENTE, index=True)
    evento_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    seccion_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cliente_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    moneda: Mapped[str] = mapped_column(String(8), nullable=False, default=MONEDA_POR_DEFECTO)
    cantidad: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Lo calcula el dominio (precio de la butaca por la cantidad), nunca el
    # cliente: la peticion del cliente no lleva monto.
    monto_total_centavos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # Huella de la peticion: el mismo `saga_id` con otra carga util es un
    # conflicto de idempotencia, no una repeticion.
    peticion_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    creada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)
    actualizada_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class PasoSaga(Base):
    """Un paso de la saga: `RESERVAR`, `COBRAR` o `EMITIR`."""

    __tablename__ = "paso_saga"

    saga_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(16), primary_key=True)
    resultado: Mapped[str] = mapped_column(String(12), nullable=False, default="")
    detalle: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    iniciado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalizado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompensacionSaga(Base):
    """La compensacion de un paso confirmado (`LIBERAR`, `REEMBOLSAR`, `ANULAR`)."""

    __tablename__ = "compensacion_saga"

    saga_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    nombre: Mapped[str] = mapped_column(String(16), primary_key=True)
    resultado: Mapped[str] = mapped_column(String(12), nullable=False, default="")
    detalle: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # Posicion en el plan de compensacion, para devolverlas en el orden en
    # que se aplicaron (el inverso de los pasos confirmados).
    orden: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finalizado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
