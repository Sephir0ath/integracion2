"""Domain logic of the `pagos` service.

It implements the saga step `COBRAR` and its compensation `REEMBOLSAR`,
both of them idempotent: the key of a command is built from the saga and
the step (`{saga_id}:COBRAR`), a replayed command returns the stored result
and never charges or refunds twice, and a key reused with a different
payload is rejected with `CONFLICT_IDEMPOTENCIA`.

The charge is made in three short steps, and **no database transaction is
open while the gateway is called**:

1. *reserve*: a short transaction claims the key of the command and checks
   whether the command was already executed;
2. *charge*: the call to the gateway, without a transaction open, so a slow
   gateway neither holds a database connection nor blocks the rest of the
   system (the Bulkhead is the one that rejects the excess);
3. *write*: a short transaction writes the payment row and the result of
   the command. If anything fails, that transaction is rolled back and no
   half written payment row is left behind.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable

from sqlalchemy.exc import IntegrityError

from common.errors import CODES, ServiceError
from common.idempotency import COMPLETADO, IdempotencyStore, hash_request
from common.logging import log_event
from common.metrics import counter

from .models import CONFIRMADO, MONEDA_POR_DEFECTO, REEMBOLSADO, Pago, ahora

logger = logging.getLogger("pagos.service")

_cobros = counter(
    "pagos_cobro_total",
    "Cobros solicitados al servicio de pagos",
    ["result"],
)

METODOS_PAGO = [
    {"metodo": "TARJETA", "descripcion": "Tarjeta de credito o debito"},
    {"metodo": "PSE", "descripcion": "Pago en linea por PSE"},
    {"metodo": "EFECTIVO", "descripcion": "Pago en efectivo en la boleteria"},
]


def payment_id_for(saga_id: str) -> str:
    """Identifier of the payment row of a saga.

    A saga charges once, so the identifier is derived from the saga: the
    row can be found again without a search, and a second charge for the
    same saga lands on the same row instead of creating another one.
    """
    return "pg_" + hashlib.sha256(saga_id.encode("utf-8")).hexdigest()[:32]


class PagosService:
    """Use cases of the payment ledger."""

    def __init__(
        self,
        session_provider: Callable[[], object],
        pasarela,
        logger_: logging.Logger | None = None,
    ) -> None:
        # `session_provider` returns a context manager that yields a
        # session and commits on success (see `common.db.session_scope`).
        self._sessions = session_provider
        self._pasarela = pasarela
        self._log = logger_ or logger

    # ------------------------------------------------------------ consulta --
    def listar_metodos_pago(self) -> list[dict]:
        """Payment methods accepted by the service."""
        return [dict(metodo) for metodo in METODOS_PAGO]

    def consultar_pago(self, saga_id: str) -> dict:
        """Read the payment of a saga."""
        with self._sessions() as session:
            pago = session.get(Pago, payment_id_for(saga_id))
            if pago is None:
                raise ServiceError(
                    CODES.NOT_FOUND, f"no hay pago para la saga '{saga_id}'", details={"saga_id": saga_id}
                )
            return pago.como_datos()

    # ---------------------------------------------------------------- cobro --
    async def cobrar(
        self,
        saga_id: str,
        monto_centavos: int,
        moneda: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Charge a saga. The same saga and step charge only once."""
        clave = idempotency_key or f"{saga_id}:COBRAR"
        moneda = moneda or MONEDA_POR_DEFECTO
        huella = hash_request(
            {"saga_id": saga_id, "monto_centavos": int(monto_centavos), "moneda": moneda}
        )

        # --- 1. reserva de la clave (transaccion corta) -------------------
        with self._sessions() as session:
            store = IdempotencyStore(session)
            previo = store.get(clave)

            if previo is not None:
                if previo["request_hash"] != huella:
                    _cobros.labels("conflicto_idempotencia").inc()
                    log_event(
                        self._log,
                        "pagos_idempotencia_conflicto",
                        clave=clave,
                        saga_id=saga_id,
                    )
                    raise ServiceError(
                        CODES.CONFLICT_IDEMPOTENCIA,
                        f"la clave '{clave}' ya fue usada con otro payload",
                        details={"clave": clave, "saga_id": saga_id},
                    )
                if previo["estado"] == COMPLETADO and previo["resultado"]:
                    _cobros.labels("idempotente").inc()
                    log_event(self._log, "pagos_cobro_idempotente", clave=clave, saga_id=saga_id)
                    return {"pago": previo["resultado"], "idempotente": True}
                # The record is EN_CURSO: a previous execution was cut short.
                # The command can run again because the gateway is idempotent
                # with the same key.
                log_event(self._log, "pagos_cobro_reanudado", clave=clave, saga_id=saga_id)
            else:
                store.reserve(clave, huella)

            existente = session.get(Pago, payment_id_for(saga_id))
            if existente is not None and existente.estado == CONFIRMADO:
                # The charge is already in the ledger: the command was
                # replayed after the idempotency record was lost.
                datos = existente.como_datos()
                store.save_result(clave, huella, datos)
                _cobros.labels("idempotente").inc()
                log_event(self._log, "pagos_cobro_idempotente", clave=clave, saga_id=saga_id)
                return {"pago": datos, "idempotente": True}

        # --- 2. el cobro, sin ninguna transaccion abierta -----------------
        try:
            respuesta = await self._pasarela.cobrar(monto_centavos, clave)
        except ServiceError as error:
            _cobros.labels(_resultado_de(error.code)).inc()
            raise

        # --- 3. escritura del pago y del resultado (transaccion corta) ----
        return self._escribir_cobro(clave, huella, saga_id, monto_centavos, moneda, respuesta)

    def _escribir_cobro(
        self,
        clave: str,
        huella: str,
        saga_id: str,
        monto_centavos: int,
        moneda: str,
        respuesta: dict,
    ) -> dict:
        """Write the payment row and the result of the command."""
        pago_id = payment_id_for(saga_id)
        try:
            with self._sessions() as session:
                store = IdempotencyStore(session)
                store.reserve(clave, huella)

                pago = session.get(Pago, pago_id)
                if pago is None:
                    pago = Pago(
                        id=pago_id,
                        saga_id=saga_id,
                        monto_centavos=int(monto_centavos),
                        moneda=moneda,
                        estado=CONFIRMADO,
                        pasarela_ref=respuesta.get("pago_id"),
                        pasarela_referencia=respuesta.get("referencia"),
                        idempotency_key=clave,
                        creado_en=ahora(),
                        actualizado_en=ahora(),
                    )
                    session.add(pago)
                    session.flush()
                datos = pago.como_datos()
                store.save_result(clave, huella, datos)
        except IntegrityError:
            # Another execution of the same command won the race: its result
            # is the result of this one.
            datos = self._leer_resultado(clave, pago_id)
            if datos is None:
                raise
            _cobros.labels("idempotente").inc()
            return {"pago": datos, "idempotente": True}

        _cobros.labels("ok").inc()
        log_event(
            self._log,
            "pagos_cobro_ok",
            saga_id=saga_id,
            clave=clave,
            pago_id=pago_id,
            monto_centavos=int(monto_centavos),
        )
        return {"pago": datos, "idempotente": False}

    # ---------------------------------------------------------- reembolso --
    async def reembolsar(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Refund the payment of a saga. A refund happens only once."""
        clave = idempotency_key or f"{saga_id}:REEMBOLSAR"
        huella = hash_request({"saga_id": saga_id, "paso": "REEMBOLSAR"})

        # --- 1. reserva de la clave (transaccion corta) -------------------
        with self._sessions() as session:
            store = IdempotencyStore(session)
            previo = store.get(clave)

            if previo is not None:
                if previo["request_hash"] != huella:
                    _cobros.labels("conflicto_idempotencia").inc()
                    raise ServiceError(
                        CODES.CONFLICT_IDEMPOTENCIA,
                        f"la clave '{clave}' ya fue usada con otro payload",
                        details={"clave": clave, "saga_id": saga_id},
                    )
                if previo["estado"] == COMPLETADO and previo["resultado"]:
                    _cobros.labels("idempotente").inc()
                    return {"pago": previo["resultado"], "idempotente": True}

            store.reserve(clave, huella)

            pago = session.get(Pago, payment_id_for(saga_id))
            if pago is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no hay pago para la saga '{saga_id}'",
                    details={"saga_id": saga_id},
                )

            if pago.estado == REEMBOLSADO:
                # Already refunded: the command is a replay.
                datos = pago.como_datos()
                store.save_result(clave, huella, datos)
                _cobros.labels("idempotente").inc()
                return {"pago": datos, "idempotente": True}

            referencia_pasarela = pago.pasarela_ref

        # --- 2. el reembolso, sin ninguna transaccion abierta -------------
        try:
            await self._pasarela.reembolsar(referencia_pasarela)
        except ServiceError as error:
            _cobros.labels(_resultado_de(error.code)).inc()
            raise

        # --- 3. escritura del estado (transaccion corta) ------------------
        with self._sessions() as session:
            store = IdempotencyStore(session)
            pago = session.get(Pago, payment_id_for(saga_id))
            if pago is None:
                raise ServiceError(
                    CODES.NOT_FOUND,
                    f"no hay pago para la saga '{saga_id}'",
                    details={"saga_id": saga_id},
                )
            pago.estado = REEMBOLSADO
            pago.actualizado_en = ahora()
            session.flush()

            datos = pago.como_datos()
            store.save_result(clave, huella, datos)

        _cobros.labels("reembolso_ok").inc()
        log_event(self._log, "pagos_reembolso_ok", saga_id=saga_id, clave=clave)
        return {"pago": datos, "idempotente": False}

    # ------------------------------------------------------- observabilidad --
    def estado_circuito(self) -> dict:
        """Live state of the protection chain around the gateway."""
        return self._pasarela.snapshot()

    def _leer_resultado(self, clave: str, pago_id: str) -> dict | None:
        """Read the result of a command that another execution wrote."""
        with self._sessions() as session:
            store = IdempotencyStore(session)
            registro = store.get(clave)
            if registro and registro["resultado"]:
                return registro["resultado"]
            pago = session.get(Pago, pago_id)
            if pago is not None:
                return pago.como_datos()
        return None


def _resultado_de(codigo: str) -> str:
    """Label of the metric for an error code."""
    return {
        CODES.CIRCUIT_OPEN: "circuit_open",
        CODES.BULKHEAD_SATURATED: "bulkhead_saturated",
        CODES.CONFLICT_IDEMPOTENCIA: "conflicto_idempotencia",
        CODES.NOT_FOUND: "no_encontrado",
        CODES.INVALID: "invalido",
    }.get(codigo, "error")
