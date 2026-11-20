"""Domain logic of the `emision` service.

It implements the saga step `EMITIR` and its compensation `ANULAR`, both
of them idempotent: the key of a command is built from the saga and the
step (`{saga_id}:EMITIR`, `{saga_id}:ANULAR`), a replayed command returns
the stored result and never issues or voids a ticket twice, and a key
reused with a different payload is rejected with `CONFLICT_IDEMPOTENCIA`.

The rule of the system lives here:

    *No se emiten entradas sin cobro exitoso.*

The verification is made **before** any ticket is written and it is made
against the `pagos` service, which owns the ledger. The payment of the
saga has to exist, has to be `CONFIRMADO` and has to be the very payment
that the caller names in the request: otherwise a caller could ask for
tickets against somebody else's payment. A `pagos` that cannot be
reached is not a confirmation either, so the answer is
`DEPENDENCY_UNAVAILABLE`.

The order of the steps is the one of `pagos`: the key of the command is
claimed in a short transaction, the dependency is called **without any
transaction open** (so a slow `pagos` neither holds the database nor
blocks the rest of the system) and the tickets are written in a last
short transaction that rolls back as a whole: a batch of tickets is
never half written.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.errors import CODES, ServiceError
from common.idempotency import COMPLETADO, IdempotencyStore, hash_request
from common.logging import log_event
from common.metrics import counter

from .faults import FalloEmision
from .models import ANULADA, EMITIDA, Entrada, ahora

logger = logging.getLogger("emision.service")

_entradas = counter(
    "emision_entradas_total",
    "Entradas emitidas y anuladas por el servicio de emision",
    ["result"],
)
_verificacion = counter(
    "emision_verificacion_pago_total",
    "Resultado de la verificacion del pago antes de emitir",
    ["result"],
)

# Las series de los contadores se inicializan en cero: asi el panel
# muestra siempre los cuatro resultados aunque todavia no haya ocurrido
# ninguno.
for _resultado in ("ok", "idempotente", "error", "anulada"):
    _entradas.labels(_resultado)
for _resultado in ("confirmado", "no_confirmado", "pago_distinto", "pagos_inalcanzable"):
    _verificacion.labels(_resultado)

# State of the payment that authorises the emission.
CONFIRMADO = "CONFIRMADO"

# State of the answer when the saga has no tickets.
NO_ENCONTRADA = "NO_ENCONTRADA"

# Prefix of the payload of the QR.
PREFIJO_QR = "TKT-"


# ---------------------------------------------------------------- helpers --
def entrada_id_for(saga_id: str, butaca_id: str) -> str:
    """Identifier of the ticket of a seat of a saga.

    A saga emits a seat once, so the identifier is derived from the saga
    and the seat: the row is found again without a search and a second
    emission of the same seat lands on the same row instead of creating
    another one.
    """
    return "ent_" + hashlib.sha256(f"{saga_id}|{butaca_id}".encode("utf-8")).hexdigest()[:32]


def calcular_qr(saga_id: str, butaca_id: str) -> str:
    """Payload of the QR of a ticket.

    It is deterministic (the same ticket always yields the same payload)
    and verifiable: the checksum is the truncated SHA-256 of the saga and
    the seat, so anybody can recompute it from the payload itself and
    check that the ticket was not tampered with.
    """
    base = f"{saga_id}-{butaca_id}"
    checksum = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"{PREFIJO_QR}{base}-{checksum}"


def verificar_qr(qr: str, saga_id: str | None = None, butaca_id: str | None = None) -> bool:
    """Check the checksum of a QR payload.

    The checksum is recomputed from the payload, so a payload that was
    changed does not verify. When the saga and the seat are given, the
    payload has to belong to them too.
    """
    if not qr or not qr.startswith(PREFIJO_QR):
        return False
    cuerpo = qr[len(PREFIJO_QR) :]
    separador = cuerpo.rfind("-")
    if separador < 0:
        return False
    base = cuerpo[:separador]
    checksum = cuerpo[separador + 1 :]
    if len(checksum) != 8:
        return False
    if hashlib.sha256(base.encode("utf-8")).hexdigest()[:8] != checksum:
        return False
    if saga_id is not None and butaca_id is not None:
        return base == f"{saga_id}-{butaca_id}"
    return True


def _datos_de_entradas(entradas: Iterable[Entrada]) -> list[dict]:
    """The tickets as plain dicts."""
    return [entrada.como_datos() for entrada in entradas]


# ---------------------------------------------------------------- servicio --
class EmisionService:
    """Use cases of the ticket emission."""

    def __init__(
        self,
        session_provider: Callable[[], object],
        pagos_client,
        logger_: logging.Logger | None = None,
        faults=None,
    ) -> None:
        # `session_provider` returns a context manager that yields a
        # session and commits on success (see `common.db.session_scope`).
        self._sessions = session_provider
        self._pagos = pagos_client
        self._log = logger_ or logger
        self._faults = faults

    # ------------------------------------------------------------- emision --
    async def emitir(
        self,
        saga_id: str,
        pago_id: str,
        butacas: Iterable[dict],
        idempotency_key: str | None = None,
    ) -> dict:
        """Issue one ticket per seat (saga step `EMITIR`)."""
        elegidas = self._validar(saga_id, pago_id, butacas)

        if self._faults is not None:
            await self._aplicar_falla()

        clave = idempotency_key or f"{saga_id}:EMITIR"
        huella = hash_request(
            {
                "saga_id": saga_id,
                "pago_id": pago_id,
                # El orden de las butacas no cambia el comando.
                "butacas": sorted(butaca["butaca_id"] for butaca in elegidas),
            }
        )

        # --- 1. reserva de la clave (transaccion corta) -------------------
        with self._sessions() as session:
            store = IdempotencyStore(session)
            previo = store.get(clave)

            if previo is not None:
                if previo["request_hash"] != huella:
                    _entradas.labels("error").inc()
                    log_event(
                        self._log,
                        "emision_idempotencia_conflicto",
                        clave=clave,
                        saga_id=saga_id,
                    )
                    raise ServiceError(
                        CODES.CONFLICT_IDEMPOTENCIA,
                        f"la clave '{clave}' ya fue usada con otro payload",
                        details={"clave": clave, "saga_id": saga_id},
                    )
                if previo["estado"] == COMPLETADO and previo["resultado"]:
                    _entradas.labels("idempotente").inc()
                    log_event(self._log, "emision_emitir_idempotente", clave=clave, saga_id=saga_id)
                    guardado = previo["resultado"]
                    return {
                        "entradas": guardado["entradas"],
                        "estado": guardado["estado"],
                        "idempotente": True,
                    }
            else:
                store.reserve(clave, huella)

        # --- 2. el cobro se verifica, sin ninguna transaccion abierta -----
        await self._verificar_pago(saga_id, pago_id)

        # --- 3. escritura de las entradas (transaccion corta) -------------
        return self._escribir_entradas(clave, huella, saga_id, elegidas)

    async def _verificar_pago(self, saga_id: str, pago_id: str) -> dict:
        """Check that the payment of the saga is confirmed.

        Three answers are possible and only one of them authorises the
        emission: the payment exists, it is `CONFIRMADO` and it is the
        payment that the caller named.
        """
        try:
            pago = await self._pagos.consultar_pago(saga_id)
        except ServiceError as error:
            if error.code == CODES.NOT_FOUND:
                # La saga no tiene pago: no hay cobro exitoso.
                _verificacion.labels("no_confirmado").inc()
                _entradas.labels("error").inc()
                log_event(
                    self._log, "emision_pago_inexistente", saga_id=saga_id, mensaje=error.message
                )
                raise ServiceError(
                    CODES.PAYMENT_REQUIRED,
                    f"la saga '{saga_id}' no tiene un pago confirmado",
                    details={"saga_id": saga_id},
                ) from error
            if error.code == CODES.DEPENDENCY_UNAVAILABLE:
                # Un servicio de pagos inalcanzable no es una confirmacion.
                _verificacion.labels("pagos_inalcanzable").inc()
                _entradas.labels("error").inc()
                log_event(
                    self._log,
                    "emision_pagos_inalcanzable",
                    saga_id=saga_id,
                    mensaje=error.message,
                )
                raise
            _entradas.labels("error").inc()
            raise

        estado = (pago.get("estado") or "").upper()
        if estado != CONFIRMADO:
            _verificacion.labels("no_confirmado").inc()
            _entradas.labels("error").inc()
            log_event(self._log, "emision_pago_no_confirmado", saga_id=saga_id, estado=estado)
            raise ServiceError(
                CODES.PAYMENT_REQUIRED,
                f"el pago de la saga '{saga_id}' no esta confirmado (estado: {estado or 'desconocido'})",
                details={"saga_id": saga_id, "estado": estado},
            )

        confirmado = pago.get("pago_id") or ""
        if not confirmado or confirmado != pago_id:
            # El pago nombrado no es el pago confirmado de la saga: no se
            # emite contra el pago de otro.
            _verificacion.labels("pago_distinto").inc()
            _entradas.labels("error").inc()
            log_event(
                self._log,
                "emision_pago_distinto",
                saga_id=saga_id,
                pago_id=pago_id,
                pago_confirmado=confirmado,
            )
            raise ServiceError(
                CODES.PAYMENT_REQUIRED,
                "el pago indicado no es el pago confirmado de la saga",
                details={
                    "saga_id": saga_id,
                    "pago_id": pago_id,
                    "pago_confirmado": confirmado,
                },
            )

        _verificacion.labels("confirmado").inc()
        log_event(self._log, "emision_pago_confirmado", saga_id=saga_id, pago_id=confirmado)
        return pago

    def _escribir_entradas(
        self, clave: str, huella: str, saga_id: str, butacas: list[dict]
    ) -> dict:
        """Write the tickets of a saga and the result of the command."""
        datos: list[dict] = []
        try:
            with self._sessions() as session:
                store = IdempotencyStore(session)
                store.reserve(clave, huella)

                for butaca in butacas:
                    entrada = Entrada(
                        id=entrada_id_for(saga_id, butaca["butaca_id"]),
                        saga_id=saga_id,
                        butaca_id=butaca["butaca_id"],
                        seccion_id=butaca["seccion_id"],
                        fila=butaca["fila"],
                        numero=int(butaca["numero"]),
                        qr=calcular_qr(saga_id, butaca["butaca_id"]),
                        estado=EMITIDA,
                        emitida_en=ahora(),
                    )
                    session.add(entrada)
                    session.flush()
                    datos.append(entrada.como_datos())

                store.save_result(clave, huella, {"entradas": datos, "estado": EMITIDA})
        except IntegrityError:
            # Otra ejecucion de la misma orden gano la carrera: sus
            # entradas son las entradas de esta orden.
            existentes = self._entradas_de(saga_id)
            if not existentes:
                raise
            with self._sessions() as session:
                store = IdempotencyStore(session)
                store.save_result(clave, huella, {"entradas": existentes, "estado": EMITIDA})
            _entradas.labels("idempotente").inc()
            log_event(self._log, "emision_emitir_idempotente", clave=clave, saga_id=saga_id)
            return {"entradas": existentes, "estado": EMITIDA, "idempotente": True}

        _entradas.labels("ok").inc()
        log_event(
            self._log,
            "emision_entradas_ok",
            saga_id=saga_id,
            clave=clave,
            entradas=len(datos),
        )
        return {"entradas": datos, "estado": EMITIDA, "idempotente": False}

    # ----------------------------------------------------------- anulacion --
    async def anular(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Void the tickets of a saga (compensation `ANULAR`).

        Voiding a ticket does not need a confirmed payment: it is the
        compensation of the emission, and it has to work even when the
        payment was refunded in the meantime.
        """
        if not saga_id or not str(saga_id).strip():
            raise ServiceError(
                CODES.INVALID, "la saga es obligatoria", details={"saga_id": saga_id}
            )

        clave = idempotency_key or f"{saga_id}:ANULAR"
        huella = hash_request({"saga_id": saga_id, "paso": "ANULAR"})

        with self._sessions() as session:
            store = IdempotencyStore(session)
            previo = store.get(clave)

            if previo is not None:
                if previo["request_hash"] != huella:
                    _entradas.labels("error").inc()
                    log_event(
                        self._log,
                        "emision_idempotencia_conflicto",
                        clave=clave,
                        saga_id=saga_id,
                    )
                    raise ServiceError(
                        CODES.CONFLICT_IDEMPOTENCIA,
                        f"la clave '{clave}' ya fue usada con otro payload",
                        details={"clave": clave, "saga_id": saga_id},
                    )
                if previo["estado"] == COMPLETADO and previo["resultado"]:
                    _entradas.labels("idempotente").inc()
                    guardado = previo["resultado"]
                    return {
                        "entradas": guardado["entradas"],
                        "estado": guardado["estado"],
                        "idempotente": True,
                    }
            else:
                store.reserve(clave, huella)

            entradas = self._leer_entradas(session, saga_id)
            if not entradas:
                estado = NO_ENCONTRADA
                datos: list[dict] = []
            else:
                momento = ahora()
                for entrada in entradas:
                    if entrada.estado != ANULADA:
                        entrada.estado = ANULADA
                        entrada.anulada_en = momento
                session.flush()
                datos = _datos_de_entradas(entradas)
                estado = ANULADA

            store.save_result(clave, huella, {"entradas": datos, "estado": estado})

        _entradas.labels("anulada").inc()
        log_event(
            self._log,
            "emision_anulacion_ok",
            saga_id=saga_id,
            clave=clave,
            estado=estado,
            entradas=len(datos),
        )
        return {"entradas": datos, "estado": estado, "idempotente": False}

    # ------------------------------------------------------------- consulta --
    def listar(self, saga_id: str) -> dict:
        """The tickets of a saga, with their state."""
        with self._sessions() as session:
            entradas = self._leer_entradas(session, saga_id)
            datos = _datos_de_entradas(entradas)

        if not datos:
            estado = NO_ENCONTRADA
        elif all(dato["estado"] == ANULADA for dato in datos):
            estado = ANULADA
        else:
            estado = EMITIDA
        return {"entradas": datos, "estado": estado, "idempotente": False}

    # ------------------------------------------------------------- interno --
    def _leer_entradas(self, session, saga_id: str) -> list[Entrada]:
        """The tickets of a saga, ordered by seat."""
        return list(
            session.execute(
                select(Entrada)
                .where(Entrada.saga_id == saga_id)
                .order_by(Entrada.fila, Entrada.numero, Entrada.butaca_id)
            )
            .scalars()
            .all()
        )

    def _entradas_de(self, saga_id: str) -> list[dict]:
        """The tickets of a saga, as plain dicts."""
        with self._sessions() as session:
            return _datos_de_entradas(self._leer_entradas(session, saga_id))

    def _validar(self, saga_id: str, pago_id: str, butacas: Iterable[dict]) -> list[dict]:
        """Check the request of the emission."""
        if not saga_id or not str(saga_id).strip():
            raise ServiceError(
                CODES.INVALID, "la saga es obligatoria", details={"saga_id": saga_id}
            )
        if not pago_id or not str(pago_id).strip():
            raise ServiceError(
                CODES.INVALID, "el pago es obligatorio", details={"pago_id": pago_id}
            )

        elegidas = list(butacas or [])
        if not elegidas:
            raise ServiceError(
                CODES.INVALID,
                "no hay butacas que emitir",
                details={"saga_id": saga_id},
            )

        vistas: set[str] = set()
        validas: list[dict] = []
        for butaca in elegidas:
            butaca_id = str((butaca or {}).get("butaca_id") or "").strip()
            if not butaca_id:
                raise ServiceError(
                    CODES.INVALID,
                    "una butaca del pedido no tiene identificador",
                    details={"saga_id": saga_id, "butaca": butaca},
                )
            if butaca_id in vistas:
                raise ServiceError(
                    CODES.INVALID,
                    f"la butaca '{butaca_id}' aparece dos veces en el pedido",
                    details={"saga_id": saga_id, "butaca_id": butaca_id},
                )
            vistas.add(butaca_id)
            try:
                numero = int((butaca or {}).get("numero") or 0)
            except (TypeError, ValueError) as error:
                raise ServiceError(
                    CODES.INVALID,
                    f"el numero de la butaca '{butaca_id}' no es un entero",
                    details={"saga_id": saga_id, "butaca_id": butaca_id},
                ) from error
            if numero < 0:
                raise ServiceError(
                    CODES.INVALID,
                    f"el numero de la butaca '{butaca_id}' no puede ser negativo",
                    details={"saga_id": saga_id, "butaca_id": butaca_id},
                )
            validas.append(
                {
                    "butaca_id": butaca_id,
                    "seccion_id": str((butaca or {}).get("seccion_id") or ""),
                    "fila": str((butaca or {}).get("fila") or ""),
                    "numero": numero,
                }
            )
        return validas

    async def _aplicar_falla(self) -> None:
        """Apply the injected fault, if there is one."""
        if self._faults is None:
            return
        try:
            await self._faults.aplicar()
        except FalloEmision as error:
            _entradas.labels("error").inc()
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"degradado: el servicio de emision no esta disponible (modo {error.modo})",
                details={"modo": error.modo},
            ) from error
