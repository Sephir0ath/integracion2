"""Domain logic of the `butacas` service.

It implements the saga step `RESERVAR` and its compensation `LIBERAR`, both
of them idempotent: the key of a command is built from the saga and the
step (`{saga_id}:RESERVAR`, `{saga_id}:LIBERAR`), a replayed command
returns the stored result and never reserves or frees twice, and a key
reused with a different payload is rejected with `CONFLICT_IDEMPOTENCIA`.

The rule of the system lives here:

    *Una butaca no se vende dos veces.*

It is enforced **by the database, atomically**. The reservation of a set of
seats is one single statement (`UPDATE ... RETURNING`) that changes to
`RESERVADA` only the rows that are still `LIBRE` and returns exactly the
rows it claimed. If it returns fewer rows than the ones that were asked
for, another saga claimed one of them in between: the whole reservation is
rejected with `ALREADY_EXISTS` and the transaction rolls back, so no seat
is left half reserved. A `SELECT` followed by an `UPDATE` would always
leave a window open between the two statements, and two sagas could claim
the same seat inside it.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.errors import CODES, ServiceError
from common.idempotency import COMPLETADO, IdempotencyStore, hash_request
from common.logging import log_event
from common.metrics import counter, gauge

from .models import (
    ACTIVA,
    BUTACAS_POR_FILA,
    ESTADOS_BUTACA,
    LIBERADA,
    LIBRE,
    RESERVADA,
    VENDIDA,
    Butaca,
    Evento,
    Reserva,
    Seccion,
    ahora,
)

logger = logging.getLogger("butacas.service")

_reservas = counter(
    "butacas_reservas_total",
    "Reservas solicitadas al servicio de butacas",
    ["result"],
)
_butacas_por_estado = gauge(
    "butacas_butacas_por_estado",
    "Butacas por estado",
    ["estado"],
)

# Las series del contador se inicializan en cero: asi el panel muestra
# siempre los cinco resultados aunque todavia no haya ocurrido ninguno.
for _resultado in ("ok", "idempotente", "error", "liberada", "vendida"):
    _reservas.labels(_resultado)
for _estado in ESTADOS_BUTACA:
    _butacas_por_estado.labels(estado=_estado)


# ---------------------------------------------------------------- helpers --
def reserva_id_for(saga_id: str) -> str:
    """Identifier of the reservation of a saga.

    A saga reserves once, so the identifier is derived from the saga: the
    row is found again without a search and a second reservation of the same
    saga lands on the same row.
    """
    return "rs_" + hashlib.sha256(saga_id.encode("utf-8")).hexdigest()[:32]


def parsear_fecha(texto: str) -> datetime:
    """Parse an ISO-8601 date into an aware UTC datetime."""
    if not texto or not str(texto).strip():
        raise ServiceError(CODES.INVALID, "la fecha es obligatoria", details={"fecha": texto})
    valor = str(texto).strip()
    if valor.endswith(("Z", "z")):
        valor = valor[:-1] + "+00:00"
    try:
        fecha = datetime.fromisoformat(valor)
    except ValueError as error:
        raise ServiceError(
            CODES.INVALID, f"la fecha '{texto}' no es ISO-8601", details={"fecha": texto}
        ) from error
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return fecha


def a_iso(valor: datetime | None) -> str:
    """Render a timestamp as ISO-8601 (UTC)."""
    if valor is None:
        return ""
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=timezone.utc)
    return valor.isoformat()


def crear_butacas(seccion_id: str, capacidad: int) -> list[dict]:
    """Seat rows of a section: eight seats per row, continuous numbering.

    The identifier of a seat is `{seccion_id}-{fila}{numero}` and the
    numbers run from 1 without restarting on every row.
    """
    filas: list[dict] = []
    for numero in range(1, int(capacidad) + 1):
        fila = chr(ord("A") + (numero - 1) // BUTACAS_POR_FILA)
        filas.append(
            {
                "butaca_id": f"{seccion_id}-{fila}{numero}",
                "seccion_id": seccion_id,
                "fila": fila,
                "numero": numero,
                "estado": LIBRE,
                "saga_id": None,
            }
        )
    return filas


def reclamo_atomico(
    session: Session, saga_id: str, seccion_id: str, butaca_ids: Iterable[str]
) -> list[str]:
    """Claim a set of seats in one atomic statement.

    This is the guarantee of the rule "una butaca no se vende dos veces":
    every row changes to `RESERVADA` only if it is still `LIBRE`, and the
    `RETURNING` says exactly which rows were claimed. If the statement
    returns fewer rows than the ones that were asked for, someone else
    claimed one of them in between and the reservation has to be rejected.
    """
    identificadores = [str(butaca_id) for butaca_id in butaca_ids]
    if not identificadores:
        return []
    enunciado = (
        update(Butaca)
        .where(
            Butaca.butaca_id.in_(identificadores),
            Butaca.seccion_id == seccion_id,
            Butaca.estado == LIBRE,
        )
        .values(estado=RESERVADA, saga_id=saga_id)
        .returning(Butaca.butaca_id)
        .execution_options(synchronize_session=False)
    )
    return [str(butaca_id) for butaca_id in session.execute(enunciado).scalars().all()]


def liberacion_atomica(session: Session, saga_id: str) -> list[str]:
    """Free the seats that the saga still holds in state `RESERVADA`.

    Seats already in state `VENDIDA` are not touched: a sold seat is not
    freed by a late compensation.
    """
    enunciado = (
        update(Butaca)
        .where(Butaca.saga_id == saga_id, Butaca.estado == RESERVADA)
        .values(estado=LIBRE, saga_id=None)
        .returning(Butaca.butaca_id)
        .execution_options(synchronize_session=False)
    )
    return [str(butaca_id) for butaca_id in session.execute(enunciado).scalars().all()]


def confirmacion_atomica(session: Session, saga_id: str) -> list[str]:
    """Close the sale of the seats that the saga holds in state `RESERVADA`."""
    enunciado = (
        update(Butaca)
        .where(Butaca.saga_id == saga_id, Butaca.estado == RESERVADA)
        .values(estado=VENDIDA)
        .returning(Butaca.butaca_id)
        .execution_options(synchronize_session=False)
    )
    return [str(butaca_id) for butaca_id in session.execute(enunciado).scalars().all()]


def _datos_evento(evento: Evento) -> dict:
    return {
        "evento_id": evento.evento_id,
        "nombre": evento.nombre,
        "lugar": evento.lugar or "",
        "fecha": a_iso(evento.fecha),
    }


def _datos_seccion(seccion: Seccion, disponibles: int) -> dict:
    return {
        "seccion_id": seccion.seccion_id,
        "evento_id": seccion.evento_id,
        "nombre": seccion.nombre,
        "capacidad_total": int(seccion.capacidad_total),
        "cantidad_disponible": int(disponibles),
        "precio_centavos": int(seccion.precio_centavos),
    }


def _datos_butaca(butaca: Butaca) -> dict:
    return {
        "butaca_id": butaca.butaca_id,
        "seccion_id": butaca.seccion_id,
        "fila": butaca.fila,
        "numero": int(butaca.numero),
        "estado": butaca.estado,
        "saga_id": butaca.saga_id or "",
    }


def _datos_reserva(reserva: Reserva, butacas: list[dict]) -> dict:
    return {
        "reserva_id": reserva.reserva_id,
        "saga_id": reserva.saga_id,
        "seccion_id": reserva.seccion_id,
        "estado": reserva.estado,
        "precio_centavos": int(reserva.precio_centavos),
        "creada_en": a_iso(reserva.creada_en),
        "butacas": butacas,
    }


def _conteo_por_estado(session: Session) -> dict[str, int]:
    """Number of seats of each state, read from the real rows."""
    filas = session.execute(
        select(Butaca.estado, func.count(Butaca.butaca_id)).group_by(Butaca.estado)
    ).all()
    return {str(estado): int(total) for estado, total in filas}


def _publicar_gauge(conteo: dict[str, int]) -> None:
    """Refresh the gauge of seats per state."""
    for estado in ESTADOS_BUTACA:
        _butacas_por_estado.labels(estado=estado).set(int(conteo.get(estado, 0)))


def _disponibles(session: Session) -> dict[str, int]:
    """Free seats per section, counted from the real seat rows."""
    filas = session.execute(
        select(Butaca.seccion_id, func.count(Butaca.butaca_id))
        .where(Butaca.estado == LIBRE)
        .group_by(Butaca.seccion_id)
    ).all()
    return {str(seccion_id): int(total) for seccion_id, total in filas}


# ---------------------------------------------------------------- servicio --
class ButacasService:
    """Use cases of the seat inventory."""

    def __init__(
        self,
        session_provider: Callable[[], object],
        logger_: logging.Logger | None = None,
    ) -> None:
        # `session_provider` returns a context manager that yields a
        # session and commits on success (see `common.db.session_scope`).
        self._sessions = session_provider
        self._log = logger_ or logger

    # ------------------------------------------------------------ consulta --
    def listar_eventos(self) -> list[dict]:
        """The events of the venue, ordered by date."""
        with self._sessions() as session:
            eventos = session.execute(
                select(Evento).order_by(Evento.fecha, Evento.evento_id)
            ).scalars().all()
            return [_datos_evento(evento) for evento in eventos]

    def listar_secciones(self, evento_id: str | None = None) -> list[dict]:
        """The sections of an event, with the free seats counted from the rows."""
        with self._sessions() as session:
            consulta = select(Seccion)
            if evento_id:
                consulta = consulta.where(Seccion.evento_id == evento_id)
            secciones = session.execute(
                consulta.order_by(Seccion.seccion_id)
            ).scalars().all()
            libres = _disponibles(session)
            return [
                _datos_seccion(seccion, libres.get(seccion.seccion_id, 0)) for seccion in secciones
            ]

    def listar_butacas(self, seccion_id: str) -> list[dict]:
        """The seating plan of a section: every seat with its row, number and state."""
        with self._sessions() as session:
            if session.get(Seccion, seccion_id) is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe la seccion '{seccion_id}'",
                    details={"seccion_id": seccion_id},
                )
            butacas = session.execute(
                select(Butaca)
                .where(Butaca.seccion_id == seccion_id)
                .order_by(Butaca.fila, Butaca.numero)
            ).scalars().all()
            return [_datos_butaca(butaca) for butaca in butacas]

    # ------------------------------------------------------------ reserva --
    def reservar(
        self,
        saga_id: str,
        evento_id: str,
        seccion_id: str,
        butaca_ids: Iterable[str],
        idempotency_key: str | None = None,
    ) -> dict:
        """Reserve the exact seats chosen by the buyer (saga step `RESERVAR`)."""
        clave = idempotency_key or f"{saga_id}:RESERVAR"
        elegidas = [str(butaca_id) for butaca_id in (butaca_ids or [])]
        huella = hash_request(
            {
                "saga_id": saga_id,
                "evento_id": evento_id,
                "seccion_id": seccion_id,
                # El orden de las butacas no cambia el comando.
                "butaca_ids": sorted(elegidas),
            }
        )

        try:
            with self._sessions() as session:
                store = IdempotencyStore(session)
                previo = store.get(clave)

                if previo is not None:
                    if previo["request_hash"] != huella:
                        _reservas.labels("error").inc()
                        log_event(
                            self._log,
                            "butacas_idempotencia_conflicto",
                            clave=clave,
                            saga_id=saga_id,
                        )
                        raise ServiceError(
                            CODES.CONFLICT_IDEMPOTENCIA,
                            f"la clave '{clave}' ya fue usada con otro payload",
                            details={"clave": clave, "saga_id": saga_id},
                        )
                    if previo["estado"] == COMPLETADO and previo["resultado"]:
                        _reservas.labels("idempotente").inc()
                        log_event(
                            self._log, "butacas_reserva_idempotente", clave=clave, saga_id=saga_id
                        )
                        guardado = previo["resultado"]
                        return {
                            "reserva": guardado["reserva"],
                            "butacas": guardado["butacas"],
                            "estado": guardado["estado"],
                            "idempotente": True,
                        }
                else:
                    store.reserve(clave, huella)

                seccion = self._validar_reserva(session, saga_id, evento_id, seccion_id, elegidas)

                # El reclamo es un solo enunciado: si devuelve menos filas de
                # las pedidas, alguien se llevo una butaca en el medio.
                obtenidas = reclamo_atomico(session, saga_id, seccion_id, elegidas)
                if len(obtenidas) != len(elegidas):
                    faltantes = sorted(set(elegidas) - set(obtenidas))
                    _reservas.labels("error").inc()
                    log_event(
                        self._log,
                        "butacas_reserva_rechazada",
                        saga_id=saga_id,
                        seccion_id=seccion_id,
                        butacas=faltantes,
                    )
                    raise ServiceError(
                        CODES.ALREADY_EXISTS,
                        "algunas de las butacas ya no estan libres",
                        details={"saga_id": saga_id, "butacas": faltantes},
                    )
                session.expire_all()

                precio = int(seccion.precio_centavos)
                reserva_id = reserva_id_for(saga_id)
                reserva = session.get(Reserva, reserva_id)
                if reserva is None:
                    reserva = Reserva(
                        reserva_id=reserva_id,
                        saga_id=saga_id,
                        seccion_id=seccion_id,
                        estado=ACTIVA,
                        precio_centavos=precio,
                        creada_en=ahora(),
                    )
                    session.add(reserva)
                else:
                    reserva.estado = ACTIVA
                    reserva.seccion_id = seccion_id
                    reserva.precio_centavos = precio
                session.flush()

                butacas = session.execute(
                    select(Butaca)
                    .where(Butaca.butaca_id.in_(elegidas))
                    .order_by(Butaca.fila, Butaca.numero)
                ).scalars().all()
                datos_butacas = [_datos_butaca(butaca) for butaca in butacas]
                datos_reserva = _datos_reserva(reserva, datos_butacas)
                store.save_result(
                    clave,
                    huella,
                    {
                        "reserva": datos_reserva,
                        "butacas": datos_butacas,
                        "estado": ACTIVA,
                    },
                )
                conteo = _conteo_por_estado(session)
        except IntegrityError as error:
            # Una restriccion unica se violo: la saga ya tiene una reserva
            # (una saga solo reserva una vez) o ya tiene esa butaca tomada.
            _reservas.labels("error").inc()
            raise ServiceError(
                CODES.ALREADY_EXISTS,
                "la saga ya tiene una reserva",
                details={"saga_id": saga_id},
            ) from error

        _publicar_gauge(conteo)
        _reservas.labels("ok").inc()
        log_event(
            self._log,
            "butacas_reserva_ok",
            saga_id=saga_id,
            seccion_id=seccion_id,
            butacas=len(elegidas),
            precio_centavos=precio,
        )
        return {
            "reserva": datos_reserva,
            "butacas": datos_butacas,
            "estado": ACTIVA,
            "idempotente": False,
        }

    def _validar_reserva(
        self,
        session: Session,
        saga_id: str,
        evento_id: str,
        seccion_id: str,
        elegidas: list[str],
    ) -> Seccion:
        """Check the request before touching any seat."""
        if not elegidas:
            raise ServiceError(
                CODES.INVALID, "no hay butacas que reservar", details={"saga_id": saga_id}
            )
        if len(set(elegidas)) != len(elegidas):
            raise ServiceError(
                CODES.INVALID,
                "la peticion repite butacas",
                details={"saga_id": saga_id, "butacas": elegidas},
            )
        if session.get(Evento, evento_id) is None:
            raise ServiceError(
                CODES.NOT_FOUND,
                f"no existe el evento '{evento_id}'",
                details={"evento_id": evento_id},
            )
        seccion = session.get(Seccion, seccion_id)
        if seccion is None:
            raise ServiceError(
                CODES.NOT_FOUND,
                f"no existe la seccion '{seccion_id}'",
                details={"seccion_id": seccion_id},
            )
        if seccion.evento_id != evento_id:
            raise ServiceError(
                CODES.INVALID,
                "la seccion no pertenece al evento",
                details={"evento_id": evento_id, "seccion_id": seccion_id},
            )
        encontradas = session.execute(
            select(Butaca).where(Butaca.butaca_id.in_(elegidas))
        ).scalars().all()
        por_id = {butaca.butaca_id: butaca for butaca in encontradas}
        for butaca_id in elegidas:
            if butaca_id not in por_id:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe la butaca '{butaca_id}'",
                    details={"butaca_id": butaca_id},
                )
            if por_id[butaca_id].seccion_id != seccion_id:
                raise ServiceError(
                    CODES.INVALID,
                    f"la butaca '{butaca_id}' no pertenece a la seccion '{seccion_id}'",
                    details={"butaca_id": butaca_id, "seccion_id": seccion_id},
                )
        reserva_viva = session.execute(
            select(Reserva).where(Reserva.saga_id == saga_id)
        ).scalar_one_or_none()
        if reserva_viva is not None and reserva_viva.estado in (ACTIVA, VENDIDA):
            # Una saga tiene una sola reserva: la que ya esta viva no se
            # duplica. (La compensada si se puede volver a reservar.)
            raise ServiceError(
                CODES.ALREADY_EXISTS,
                "la saga ya tiene una reserva activa",
                details={"saga_id": saga_id, "reserva_id": reserva_viva.reserva_id},
            )
        return seccion

    # ----------------------------------------------------------- liberar --
    def liberar(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Release the seats of a saga (compensation of step 1).

        The compensation is idempotent: a replayed command returns the
        stored result and frees nothing, because the seats are already free.
        """
        clave = idempotency_key or f"{saga_id}:LIBERAR"
        huella = hash_request({"saga_id": saga_id, "paso": "LIBERAR"})

        with self._sessions() as session:
            store = IdempotencyStore(session)
            previo = store.get(clave)
            if previo is not None:
                if previo["request_hash"] != huella:
                    _reservas.labels("error").inc()
                    raise ServiceError(
                        CODES.CONFLICT_IDEMPOTENCIA,
                        f"la clave '{clave}' ya fue usada con otro payload",
                        details={"clave": clave, "saga_id": saga_id},
                    )
                if previo["estado"] == COMPLETADO and previo["resultado"]:
                    # La compensacion ya se aplico: se devuelve el resultado
                    # guardado y esta ejecucion no suelta ninguna butaca.
                    guardado = previo["resultado"]
                    _reservas.labels("idempotente").inc()
                    return {
                        "liberado": guardado["estado"] == LIBERADA,
                        "butacas_liberadas": 0,
                        "estado": guardado["estado"],
                    }
            else:
                store.reserve(clave, huella)

            reserva = session.execute(
                select(Reserva).where(Reserva.saga_id == saga_id)
            ).scalar_one_or_none()
            if reserva is None:
                # Sin reserva no hay nada que compensar: no es un error.
                # No se guarda resultado para que una reserva posterior pueda
                # liberarse con la misma clave.
                return {"liberado": False, "butacas_liberadas": 0, "estado": "NO_ENCONTRADA"}

            if reserva.estado == LIBERADA:
                store.save_result(
                    clave, huella, {"estado": LIBERADA, "reserva_id": reserva.reserva_id}
                )
                return {"liberado": True, "butacas_liberadas": 0, "estado": LIBERADA}

            liberadas = liberacion_atomica(session, saga_id)
            session.expire_all()
            if reserva.estado != VENDIDA:
                # Una venta firme no se deshace con una compensacion tardia:
                # la reserva ya vendida sigue como esta.
                reserva.estado = LIBERADA
            store.save_result(
                clave, huella, {"estado": LIBERADA, "reserva_id": reserva.reserva_id}
            )
            conteo = _conteo_por_estado(session)

        _publicar_gauge(conteo)
        _reservas.labels("liberada").inc()
        log_event(
            self._log,
            "butacas_liberacion_ok",
            saga_id=saga_id,
            butacas=len(liberadas),
        )
        return {"liberado": True, "butacas_liberadas": len(liberadas), "estado": LIBERADA}

    # --------------------------------------------------- confirmar venta --
    def confirmar_venta(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Close the sale of the seats that the saga holds.

        It is idempotent: a reservation that is already `VENDIDA` returns
        the same answer without changing anything.
        """
        clave = idempotency_key or f"{saga_id}:CONFIRMAR"
        huella = hash_request({"saga_id": saga_id, "paso": "CONFIRMAR"})

        with self._sessions() as session:
            store = IdempotencyStore(session)
            previo = store.get(clave)
            if previo is not None:
                if previo["request_hash"] != huella:
                    _reservas.labels("error").inc()
                    raise ServiceError(
                        CODES.CONFLICT_IDEMPOTENCIA,
                        f"la clave '{clave}' ya fue usada con otro payload",
                        details={"clave": clave, "saga_id": saga_id},
                    )
                if previo["estado"] == COMPLETADO and previo["resultado"]:
                    guardado = previo["resultado"]
                    _reservas.labels("idempotente").inc()
                    return {
                        "confirmada": bool(guardado["confirmada"]),
                        "butacas_confirmadas": int(guardado["butacas_confirmadas"]),
                        "estado": guardado["estado"],
                    }
            else:
                store.reserve(clave, huella)

            reserva = session.execute(
                select(Reserva).where(Reserva.saga_id == saga_id)
            ).scalar_one_or_none()
            if reserva is None:
                return {
                    "confirmada": False,
                    "butacas_confirmadas": 0,
                    "estado": "NO_ENCONTRADA",
                }

            if reserva.estado == LIBERADA:
                # La reserva ya fue compensada: no hay venta que confirmar.
                return {
                    "confirmada": False,
                    "butacas_confirmadas": 0,
                    "estado": "NO_ENCONTRADA",
                }

            if reserva.estado == VENDIDA:
                # Ya confirmada: la misma respuesta, sin tocar nada.
                confirmadas = session.execute(
                    select(func.count(Butaca.butaca_id)).where(
                        Butaca.saga_id == saga_id, Butaca.estado == VENDIDA
                    )
                ).scalar_one()
                store.save_result(
                    clave,
                    huella,
                    {
                        "confirmada": True,
                        "butacas_confirmadas": int(confirmadas),
                        "estado": VENDIDA,
                    },
                )
                return {
                    "confirmada": True,
                    "butacas_confirmadas": int(confirmadas),
                    "estado": VENDIDA,
                }

            confirmadas = confirmacion_atomica(session, saga_id)
            session.expire_all()
            reserva.estado = VENDIDA
            store.save_result(
                clave,
                huella,
                {
                    "confirmada": True,
                    "butacas_confirmadas": len(confirmadas),
                    "estado": VENDIDA,
                },
            )
            conteo = _conteo_por_estado(session)

        _publicar_gauge(conteo)
        _reservas.labels("vendida").inc()
        log_event(
            self._log,
            "butacas_venta_confirmada",
            saga_id=saga_id,
            butacas=len(confirmadas),
        )
        return {
            "confirmada": True,
            "butacas_confirmadas": len(confirmadas),
            "estado": VENDIDA,
        }

    # --------------------------------------------------------- consultar --
    def consultar_reserva(self, saga_id: str) -> dict:
        """The reservation of a saga and its seats."""
        with self._sessions() as session:
            reserva = session.execute(
                select(Reserva).where(Reserva.saga_id == saga_id)
            ).scalar_one_or_none()
            if reserva is None:
                return {"reserva": None, "butacas": [], "estado": "NO_ENCONTRADA"}
            butacas = session.execute(
                select(Butaca)
                .where(Butaca.saga_id == saga_id)
                .order_by(Butaca.fila, Butaca.numero)
            ).scalars().all()
            datos = _datos_reserva(reserva, [_datos_butaca(butaca) for butaca in butacas])
            return {"reserva": datos, "butacas": datos["butacas"], "estado": reserva.estado}

    # -------------------------------------------------------------- crud --
    def crear_evento(self, nombre: str, lugar: str = "", fecha: str = "") -> dict:
        """Create an event."""
        if not nombre or not str(nombre).strip():
            raise ServiceError(CODES.INVALID, "el nombre del evento es obligatorio")
        momento = parsear_fecha(fecha)
        evento_id = f"ev_{uuid.uuid4().hex[:12]}"
        with self._sessions() as session:
            evento = Evento(
                evento_id=evento_id,
                nombre=str(nombre).strip(),
                lugar=str(lugar or "").strip(),
                fecha=momento,
            )
            session.add(evento)
            session.flush()
            return _datos_evento(evento)

    def actualizar_evento(
        self, evento_id: str, nombre: str = "", lugar: str = "", fecha: str = ""
    ) -> dict:
        """Update an event."""
        with self._sessions() as session:
            evento = session.get(Evento, evento_id)
            if evento is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe el evento '{evento_id}'",
                    details={"evento_id": evento_id},
                )
            if nombre and str(nombre).strip():
                evento.nombre = str(nombre).strip()
            if lugar and str(lugar).strip():
                evento.lugar = str(lugar).strip()
            if fecha:
                evento.fecha = parsear_fecha(fecha)
            session.flush()
            return _datos_evento(evento)

    def eliminar_evento(self, evento_id: str) -> dict:
        """Delete an event that was not sold."""
        with self._sessions() as session:
            evento = session.get(Evento, evento_id)
            if evento is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe el evento '{evento_id}'",
                    details={"evento_id": evento_id},
                )
            secciones = session.execute(
                select(Seccion).where(Seccion.evento_id == evento_id)
            ).scalars().all()
            if secciones:
                seccion_ids = [seccion.seccion_id for seccion in secciones]
                ocupadas = session.execute(
                    select(func.count(Butaca.butaca_id)).where(
                        Butaca.seccion_id.in_(seccion_ids),
                        Butaca.estado.in_([RESERVADA, VENDIDA]),
                    )
                ).scalar_one()
                if ocupadas:
                    raise ServiceError(
                        CODES.INVALID,
                        "no se puede eliminar un evento con butacas reservadas o vendidas",
                        details={"evento_id": evento_id},
                    )
                butacas = session.execute(
                    select(Butaca).where(Butaca.seccion_id.in_(seccion_ids))
                ).scalars().all()
                for butaca in butacas:
                    session.delete(butaca)
                for seccion in secciones:
                    session.delete(seccion)
            session.delete(evento)
            session.flush()
            conteo = _conteo_por_estado(session)
        _publicar_gauge(conteo)
        return {"eliminado": True}

    def crear_seccion(
        self,
        evento_id: str,
        nombre: str,
        capacidad_total: int,
        precio_centavos: int,
    ) -> dict:
        """Create a section and the seat rows of its seating plan."""
        if not nombre or not str(nombre).strip():
            raise ServiceError(CODES.INVALID, "el nombre de la seccion es obligatorio")
        capacidad = int(capacidad_total or 0)
        if capacidad <= 0:
            raise ServiceError(
                CODES.INVALID, "la capacidad de la seccion debe ser positiva", details={"capacidad": capacidad}
            )
        precio = int(precio_centavos or 0)
        if precio < 0:
            raise ServiceError(CODES.INVALID, "el precio no puede ser negativo")
        seccion_id = f"sec_{uuid.uuid4().hex[:12]}"
        with self._sessions() as session:
            if session.get(Evento, evento_id) is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe el evento '{evento_id}'",
                    details={"evento_id": evento_id},
                )
            seccion = Seccion(
                seccion_id=seccion_id,
                evento_id=evento_id,
                nombre=str(nombre).strip(),
                capacidad_total=capacidad,
                precio_centavos=precio,
            )
            session.add(seccion)
            for butaca in crear_butacas(seccion_id, capacidad):
                session.add(Butaca(**butaca))
            session.flush()
            datos = _datos_seccion(seccion, capacidad)
            conteo = _conteo_por_estado(session)
        _publicar_gauge(conteo)
        return datos

    def actualizar_seccion(
        self,
        seccion_id: str,
        nombre: str = "",
        capacidad_total: int | None = None,
        precio_centavos: int | None = None,
    ) -> dict:
        """Update a section and adjust the rows of its seating plan."""
        with self._sessions() as session:
            seccion = session.get(Seccion, seccion_id)
            if seccion is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe la seccion '{seccion_id}'",
                    details={"seccion_id": seccion_id},
                )
            capacidad = (
                int(capacidad_total) if capacidad_total else int(seccion.capacidad_total)
            )
            if capacidad <= 0:
                raise ServiceError(CODES.INVALID, "la capacidad de la seccion debe ser positiva")
            if capacidad != int(seccion.capacidad_total):
                if capacidad < int(seccion.capacidad_total):
                    ocupadas = session.execute(
                        select(func.count(Butaca.butaca_id)).where(
                            Butaca.seccion_id == seccion_id,
                            Butaca.numero > capacidad,
                            Butaca.estado.in_([RESERVADA, VENDIDA]),
                        )
                    ).scalar_one()
                    if ocupadas:
                        raise ServiceError(
                            CODES.INVALID,
                            "no se puede reducir la capacidad de una seccion con butacas vendidas",
                            details={"seccion_id": seccion_id},
                        )
                    sobrantes = session.execute(
                        select(Butaca).where(
                            Butaca.seccion_id == seccion_id, Butaca.numero > capacidad
                        )
                    ).scalars().all()
                    for butaca in sobrantes:
                        session.delete(butaca)
                else:
                    existentes = {
                        int(butaca.numero)
                        for butaca in session.execute(
                            select(Butaca).where(Butaca.seccion_id == seccion_id)
                        ).scalars().all()
                    }
                    for butaca in crear_butacas(seccion_id, capacidad):
                        if int(butaca["numero"]) not in existentes:
                            session.add(Butaca(**butaca))
                seccion.capacidad_total = capacidad
            if nombre and str(nombre).strip():
                seccion.nombre = str(nombre).strip()
            if precio_centavos is not None:
                seccion.precio_centavos = int(precio_centavos)
            session.flush()
            libres = _disponibles(session)
            datos = _datos_seccion(seccion, libres.get(seccion_id, 0))
            conteo = _conteo_por_estado(session)
        _publicar_gauge(conteo)
        return datos

    def eliminar_seccion(self, seccion_id: str) -> dict:
        """Delete a section that was not sold."""
        with self._sessions() as session:
            seccion = session.get(Seccion, seccion_id)
            if seccion is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no existe la seccion '{seccion_id}'",
                    details={"seccion_id": seccion_id},
                )
            ocupadas = session.execute(
                select(func.count(Butaca.butaca_id)).where(
                    Butaca.seccion_id == seccion_id, Butaca.estado.in_([RESERVADA, VENDIDA])
                )
            ).scalar_one()
            if ocupadas:
                raise ServiceError(
                    CODES.INVALID,
                    "no se puede eliminar una seccion con butacas reservadas o vendidas",
                    details={"seccion_id": seccion_id},
                )
            butacas = session.execute(
                select(Butaca).where(Butaca.seccion_id == seccion_id)
            ).scalars().all()
            for butaca in butacas:
                session.delete(butaca)
            session.delete(seccion)
            session.flush()
            conteo = _conteo_por_estado(session)
        _publicar_gauge(conteo)
        return {"eliminado": True}
