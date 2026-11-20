"""Orquestador de la saga de compra de entradas.

El servicio `compras` es el unico que conoce la secuencia completa de la
compra y por eso es el dueno del estado de la saga:

    RESERVAR (butacas) -> COBRAR (pagos) -> EMITIR (emision)

Cada paso tiene su compensacion (`LIBERAR`, `REEMBOLSAR`, `ANULAR`). Cuando
un paso falla, el plan de compensacion se **deriva de los pasos que quedaron
confirmados** y se aplica en orden inverso, que es la regla del sistema
(S3.2): si falla `EMITIR` se reembolsa y despues se libera; si falla
`COBRAR` solo se libera; si falla `RESERVAR` no hay nada que compensar y la
saga queda `FALLIDA`.

Tres invariantes mas:

* **El monto lo define el dominio.** El cliente nunca manda cuanto pagar: el
  monto es el precio de la butaca que devuelve la reserva por la cantidad de
  butacas. Un cliente que pudiera fijar el monto podria comprar a cero.
* **Los pasos y las compensaciones son idempotentes.** Cada comando lleva la
  clave `{saga_id}:{PASO}` (o `{saga_id}:{COMPENSACION}`), asi que un paso
  repetido devuelve el resultado guardado y no cobra ni reserva dos veces
  (S3.3). La compra completa es idempotente por `saga_id`: la segunda
  llamada devuelve el estado persistido y no toca ninguna dependencia, y el
  mismo `saga_id` con otra carga util es `CONFLICT_IDEMPOTENCIA`.
* **Toda llamada corre con limite de tiempo** (`common.grpc.wait_for_call`
  con el plazo del orquestador). Una dependencia colgada se traduce a
  `DEPENDENCY_UNAVAILABLE` y dispara la compensacion; nunca deja la saga
  esperando.

Las transacciones son cortas: la dependencia se llama **sin ninguna
transaccion abierta** y el resultado se escribe despues, igual que en
`emision` y `pagos`. Asi una dependencia lenta no retiene la base de datos.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from common.errors import CODES, ServiceError
from common.grpc import wait_for_call
from common.idempotency import COMPLETADO, IdempotencyStore, hash_request
from common.logging import log_event
from common.metrics import counter, histogram

from .models import (
    COMPENSACION_DE_PASO,
    COMPENSACIONES,
    COMPENSADA,
    CONFIRMADA,
    ERROR,
    FALLIDA,
    MONEDA_POR_DEFECTO,
    OK,
    PASOS,
    RUNNING,
    SKIPPED,
    CompensacionSaga,
    PasoSaga,
    Saga,
    a_iso,
    ahora,
)

logger = logging.getLogger("compras.service")

# ---------------------------------------------------------------- metricas --
# Nombres y etiquetas del contrato compartido: los paneles y los scripts de
# evidencia los leen tal cual.
_sagas = counter(
    "saga_total",
    "Sagas de compra terminadas, por desenlace",
    ["outcome"],
)
_pasos = counter(
    "saga_steps_total",
    "Pasos de la saga ejecutados, por paso y resultado",
    ["step", "result"],
)
_compensaciones = counter(
    "saga_compensations_total",
    "Compensaciones aplicadas, por compensacion y resultado",
    ["step", "result"],
)
_duracion = histogram(
    "saga_duration_seconds",
    "Duracion de la compra orquestada, por desenlace",
    ["outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
)

# Las series se inicializan en cero: asi el panel muestra siempre todas las
# combinaciones aunque todavia no haya ocurrido ninguna.
RESULTADOS_SAGA = ("CONFIRMADA", "COMPENSADA", "FALLIDA", "IDEMPOTENTE")
RESULTADOS_PASO = (OK, ERROR, SKIPPED)
RESULTADOS_COMPENSACION = (OK, ERROR)

for _desenlace in RESULTADOS_SAGA:
    _sagas.labels(_desenlace)
    _duracion.labels(_desenlace)
for _paso in PASOS:
    for _resultado in RESULTADOS_PASO:
        _pasos.labels(_paso, _resultado)
for _compensacion in COMPENSACIONES:
    for _resultado in RESULTADOS_COMPENSACION:
        _compensaciones.labels(_compensacion, _resultado)


# ---------------------------------------------------------------- helpers --
def _mensaje_de(error: Exception) -> str:
    """Failure of a step as a readable text, with its machine code."""
    if isinstance(error, ServiceError):
        return f"{error.code}: {error.message}"
    texto = str(error).strip()
    return f"{type(error).__name__}: {texto}" if texto else type(error).__name__


class ComprasService:
    """Casos de uso de la compra orquestada."""

    def __init__(
        self,
        session_provider: Callable[[], object],
        butacas,
        pagos,
        emision,
        logger_: logging.Logger | None = None,
        deadline: float = 5.0,
    ) -> None:
        # `session_provider` returns a context manager that yields a
        # session and commits on success (see `common.db.session_scope`).
        self._sessions = session_provider
        self._butacas = butacas
        self._pagos = pagos
        self._emision = emision
        self._log = logger_ or logger
        self._deadline = float(deadline)

    # ------------------------------------------------------------- compra --
    async def iniciar_compra(
        self,
        saga_id: str,
        evento_id: str,
        seccion_id: str = "",
        butaca_ids: Iterable[str] | None = None,
        cliente_id: str = "",
        moneda: str | None = None,
    ) -> dict:
        """Start a purchase: run the three steps or compensate them.

        The answer is the persisted state of the saga plus `idempotente`,
        which is `True` when the answer came from a previous execution.
        """
        evento_id, seccion_id, elegidas, cliente_id, moneda = self._validar(
            saga_id, evento_id, seccion_id, butaca_ids, cliente_id, moneda
        )

        # La huella identifica la carga util del comando: el mismo `saga_id`
        # con otra carga util no es una repeticion, es un conflicto.
        huella = hash_request(
            {
                "saga_id": saga_id,
                "evento_id": evento_id,
                "seccion_id": seccion_id,
                "butaca_ids": sorted(elegidas),
                "cliente_id": cliente_id,
                "moneda": moneda,
            }
        )

        inicio = time.perf_counter()
        previo = self._reclamar(saga_id, huella, evento_id, seccion_id, elegidas, cliente_id, moneda)
        if previo is not None:
            _sagas.labels("IDEMPOTENTE").inc()
            _duracion.labels("IDEMPOTENTE").observe(time.perf_counter() - inicio)
            log_event(
                self._log,
                "saga_idempotente",
                saga_id=saga_id,
                estado=previo["estado"],
                pasos=len(previo["pasos"]),
            )
            return previo

        contexto = {
            "evento_id": evento_id,
            "seccion_id": seccion_id,
            "butaca_ids": elegidas,
            "cliente_id": cliente_id,
            "moneda": moneda,
            "cantidad": len(elegidas),
            "monto": 0,
            "butacas": [],
            "pago_id": "",
        }

        estado, error_texto = await self._ejecutar(saga_id, contexto)

        datos = self._respuesta(saga_id, idempotente=False)
        if datos is None:  # pragma: no cover - el estado se acaba de escribir
            raise ServiceError(
                CODES.NOT_FOUND,
                f"no hay estado persistido para la saga '{saga_id}'",
                details={"saga_id": saga_id},
            )

        # El resultado se guarda recien cuando el estado esta cerrado: una
        # repeticion concurrente lee `EN_CURSO` y devuelve lo persistido en
        # lugar de repetir los efectos.
        self._guardar_resultado(saga_id, huella, datos)

        _sagas.labels(estado).inc()
        _duracion.labels(estado).observe(time.perf_counter() - inicio)
        log_event(
            self._log,
            "saga_terminada",
            saga_id=saga_id,
            estado=estado,
            pasos=[paso["nombre"] for paso in datos["pasos"]],
            compensaciones=[comp["nombre"] for comp in datos["compensaciones"]],
            monto_total_centavos=datos["monto_total_centavos"],
            error=error_texto,
        )
        return datos

    # ------------------------------------------------------------ consulta --
    async def consultar_compra(self, saga_id: str) -> dict:
        """The persisted state of a saga."""
        if not saga_id or not str(saga_id).strip():
            raise ServiceError(
                CODES.INVALID, "el saga_id es obligatorio", details={"saga_id": saga_id}
            )
        datos = self._respuesta(saga_id, idempotente=False)
        if datos is None:
            raise ServiceError(
                CODES.NOT_FOUND,
                f"no hay una saga de compra con el identificador '{saga_id}'",
                details={"saga_id": saga_id},
            )
        return datos

    # ---------------------------------------------------------- la saga --
    async def _ejecutar(self, saga_id: str, contexto: dict) -> tuple[str, str]:
        """Run the steps; compensate the confirmed ones when a step fails."""
        confirmados: list[str] = []
        error_texto = ""

        for indice, nombre in enumerate(PASOS):
            self._abrir_paso(saga_id, nombre)
            try:
                detalle = await self._correr_paso(nombre, saga_id, contexto)
            except Exception as error:  # noqa: BLE001 - toda falla del paso se compensa
                error_texto = _mensaje_de(error)
                _pasos.labels(nombre, ERROR).inc()
                self._cerrar_paso(saga_id, nombre, ERROR, error_texto)
                log_event(
                    self._log,
                    "saga_paso_error",
                    saga_id=saga_id,
                    paso=nombre,
                    error=error_texto,
                )
                # Los pasos que nunca corrieron quedan explicitamente omitidos.
                self._omitir_restantes(saga_id, PASOS[indice + 1 :])
                break

            confirmados.append(nombre)
            _pasos.labels(nombre, OK).inc()
            self._cerrar_paso(saga_id, nombre, OK, detalle)
            log_event(self._log, "saga_paso_ok", saga_id=saga_id, paso=nombre, detalle=detalle)
        else:
            # Los tres pasos quedaron firmes: se cierra la venta de las butacas.
            try:
                await self._llamar(self._butacas.confirmar_venta(saga_id))
            except Exception as error:  # noqa: BLE001 - el cierre tambien se compensa
                error_texto = f"no se pudo cerrar la venta: {_mensaje_de(error)}"
                log_event(
                    self._log,
                    "saga_cierre_error",
                    saga_id=saga_id,
                    error=error_texto,
                )
                # Un cierre fallido deja la venta a medias: se compensa todo
                # para no dejar un cobro sin operacion.
                return await self._compensar(saga_id, confirmados, error_texto)

            self._cerrar_saga(saga_id, CONFIRMADA)
            log_event(
                self._log,
                "saga_confirmada",
                saga_id=saga_id,
                monto_total_centavos=contexto["monto"],
                cantidad=contexto["cantidad"],
            )
            return CONFIRMADA, ""

        if not confirmados:
            # Fallo el primer paso: no hay nada confirmado que compensar.
            self._cerrar_saga(saga_id, FALLIDA, error_texto)
            return FALLIDA, error_texto

        return await self._compensar(saga_id, confirmados, error_texto)

    async def _compensar(
        self, saga_id: str, confirmados: list[str], error_texto: str
    ) -> tuple[str, str]:
        """Apply the compensations in the reverse order of the confirmed steps.

        A compensation that fails does not stop the following ones: leaving
        the rest half compensated is worse than reporting one failure. The
        saga ends `FALLIDA` when any compensation failed, `COMPENSADA` only
        when every one of them worked.
        """
        plan = [COMPENSACION_DE_PASO[paso] for paso in reversed(confirmados)]
        fallos = 0

        for orden, nombre in enumerate(plan):
            try:
                detalle = await self._correr_compensacion(nombre, saga_id)
            except Exception as error:  # noqa: BLE001 - se reporta y se sigue
                resultado = ERROR
                detalle = _mensaje_de(error)
                fallos += 1
                log_event(
                    self._log,
                    "saga_compensacion_error",
                    saga_id=saga_id,
                    compensacion=nombre,
                    error=detalle,
                )
            else:
                resultado = OK

            _compensaciones.labels(nombre, resultado).inc()
            self._registrar_compensacion(saga_id, nombre, resultado, detalle, orden)

        estado = COMPENSADA if fallos == 0 else FALLIDA
        self._cerrar_saga(saga_id, estado, error_texto)
        log_event(
            self._log,
            "saga_compensada" if estado == COMPENSADA else "saga_compensacion_incompleta",
            saga_id=saga_id,
            estado=estado,
            compensaciones=plan,
            fallos=fallos,
        )
        return estado, error_texto

    # ---------------------------------------------------- pasos concretos --
    async def _correr_paso(self, nombre: str, saga_id: str, contexto: dict) -> str:
        """Run one step against its dependency and return the detail."""
        if nombre == "RESERVAR":
            datos = await self._llamar(
                self._butacas.reservar(
                    saga_id,
                    contexto["evento_id"],
                    contexto["seccion_id"],
                    contexto["butaca_ids"],
                    idempotency_key=f"{saga_id}:RESERVAR",
                )
            )
            butacas = list(datos.get("butacas") or [])
            precio = int((datos.get("reserva") or {}).get("precio_centavos") or 0)
            cantidad = len(butacas) or contexto["cantidad"]
            monto = precio * cantidad
            contexto["butacas"] = butacas
            contexto["cantidad"] = cantidad
            contexto["monto"] = monto
            self._guardar_reserva(saga_id, monto, cantidad)
            return f"{cantidad} butacas reservadas por {monto} centavos"

        if nombre == "COBRAR":
            datos = await self._llamar(
                self._pagos.cobrar(
                    saga_id,
                    contexto["monto"],
                    contexto["moneda"],
                    idempotency_key=f"{saga_id}:COBRAR",
                )
            )
            pago = datos.get("pago") or {}
            pago_id = pago.get("pago_id") or ""
            if not pago_id:
                # Un cobro sin identificador no autoriza la emision: sin
                # pago no hay entrada.
                raise ServiceError(
                    CODES.PAYMENT_REQUIRED,
                    "el cobro no devolvio un pago confirmado",
                    details={"saga_id": saga_id, "estado": pago.get("estado", "")},
                )
            contexto["pago_id"] = pago_id
            return f"pago {pago_id} por {contexto['monto']} centavos"

        if nombre == "EMITIR":
            datos = await self._llamar(
                self._emision.emitir(
                    saga_id,
                    contexto["pago_id"],
                    contexto["butacas"],
                    idempotency_key=f"{saga_id}:EMITIR",
                )
            )
            entradas = list(datos.get("entradas") or [])
            return f"{len(entradas)} entradas emitidas"

        raise ServiceError(  # pragma: no cover - PASOS es una constante
            CODES.INVALID, f"paso desconocido: {nombre}", details={"saga_id": saga_id}
        )

    async def _correr_compensacion(self, nombre: str, saga_id: str) -> str:
        """Run one compensation against its dependency and return the detail."""
        if nombre == "LIBERAR":
            datos = await self._llamar(
                self._butacas.liberar(saga_id, idempotency_key=f"{saga_id}:LIBERAR")
            )
            return f"{int(datos.get('butacas_liberadas') or 0)} butacas liberadas"

        if nombre == "REEMBOLSAR":
            datos = await self._llamar(
                self._pagos.reembolsar(saga_id, idempotency_key=f"{saga_id}:REEMBOLSAR")
            )
            pago = datos.get("pago") or {}
            return f"pago {pago.get('pago_id') or 'sin identificar'} reembolsado"

        if nombre == "ANULAR":
            datos = await self._llamar(
                self._emision.anular(saga_id, idempotency_key=f"{saga_id}:ANULAR")
            )
            anuladas = len(datos.get("entradas") or [])
            return f"{anuladas} entradas anuladas"

        raise ServiceError(  # pragma: no cover - COMPENSACION_DE_PASO es constante
            CODES.INVALID, f"compensacion desconocida: {nombre}", details={"saga_id": saga_id}
        )

    async def _llamar(self, coro):
        """Await a dependency under the time limit of the orchestrator."""
        try:
            return await wait_for_call(coro, timeout=self._deadline)
        except asyncio.TimeoutError as error:
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"la dependencia no respondio dentro del limite de {self._deadline}s",
                details={"deadline": self._deadline},
            ) from error

    # ------------------------------------------------------- persistencia --
    def _reclamar(
        self,
        saga_id: str,
        huella: str,
        evento_id: str,
        seccion_id: str,
        butaca_ids: list[str],
        cliente_id: str,
        moneda: str,
    ) -> dict | None:
        """Claim the command; return the saved answer when it is a replay.

        The claim lives in the shared idempotency table of the service, so
        two concurrent calls with the same `saga_id` cannot both run the
        saga: the second one finds the key taken.
        """
        try:
            with self._sessions() as session:
                store = IdempotencyStore(session)
                previo = store.get(saga_id)

                if previo is not None:
                    if previo["request_hash"] != huella:
                        raise self._conflicto(saga_id)
                    if previo["estado"] == COMPLETADO and previo["resultado"]:
                        return {**previo["resultado"], "idempotente": True}
                    # El registro quedo `EN_CURSO`: una ejecucion anterior se
                    # corto a medias. Si dejo estado, se devuelve ese estado y
                    # no se repiten efectos que ya pudieron ocurrir; si no
                    # dejo ninguno, el comando se reanuda.
                    existente = session.get(Saga, saga_id)
                    if existente is not None:
                        return self._datos(session, existente, idempotente=True)
                else:
                    store.reserve(saga_id, huella)

                saga = Saga(
                    saga_id=saga_id,
                    estado=RUNNING,
                    evento_id=evento_id,
                    seccion_id=seccion_id,
                    cliente_id=cliente_id,
                    moneda=moneda,
                    cantidad=len(butaca_ids),
                    monto_total_centavos=0,
                    error="",
                    peticion_hash=huella,
                    creada_en=ahora(),
                    actualizada_en=ahora(),
                )
                session.add(saga)
                for orden, nombre in enumerate(PASOS):
                    session.add(
                        PasoSaga(
                            saga_id=saga_id,
                            nombre=nombre,
                            resultado="",
                            detalle="",
                            orden=orden,
                        )
                    )
                session.flush()
                return None
        except IntegrityError:
            # Otra ejecucion de la misma orden gano la carrera: su estado es
            # el estado de esta orden.
            estado_previo = self._estado_persistido(saga_id)
            if estado_previo is None:
                raise
            peticion_hash, datos = estado_previo
            if peticion_hash != huella:
                raise self._conflicto(saga_id) from None
            _sagas.labels("IDEMPOTENTE").inc()
            return datos

    def _conflicto(self, saga_id: str) -> ServiceError:
        """The error of a `saga_id` reused with a different payload."""
        log_event(self._log, "saga_idempotencia_conflicto", saga_id=saga_id)
        return ServiceError(
            CODES.CONFLICT_IDEMPOTENCIA,
            f"el saga_id '{saga_id}' ya fue usado con otra peticion",
            details={"saga_id": saga_id},
        )

    def _abrir_paso(self, saga_id: str, nombre: str) -> None:
        """Mark a step as started."""
        momento = ahora()
        with self._sessions() as session:
            paso = session.get(PasoSaga, (saga_id, nombre))
            if paso is not None:
                paso.iniciado_en = momento
                session.flush()
            saga = session.get(Saga, saga_id)
            if saga is not None:
                saga.estado = RUNNING
                saga.actualizada_en = momento

    def _cerrar_paso(self, saga_id: str, nombre: str, resultado: str, detalle: str) -> None:
        """Close a step with its result."""
        momento = ahora()
        with self._sessions() as session:
            paso = session.get(PasoSaga, (saga_id, nombre))
            if paso is not None:
                paso.resultado = resultado
                paso.detalle = detalle[:512]
                paso.finalizado_en = momento
                session.flush()
            saga = session.get(Saga, saga_id)
            if saga is not None:
                saga.actualizada_en = momento

    def _omitir_restantes(self, saga_id: str, nombres: Iterable[str]) -> None:
        """Mark the steps that never ran as `SKIPPED`."""
        momento = ahora()
        with self._sessions() as session:
            for nombre in nombres:
                paso = session.get(PasoSaga, (saga_id, nombre))
                if paso is not None and not paso.resultado:
                    paso.resultado = SKIPPED
                    paso.detalle = "no se ejecuto: la saga fallo antes"
                    paso.finalizado_en = momento
                    _pasos.labels(nombre, SKIPPED).inc()
            saga = session.get(Saga, saga_id)
            if saga is not None:
                saga.actualizada_en = momento

    def _guardar_reserva(self, saga_id: str, monto: int, cantidad: int) -> None:
        """Persist the amount of the purchase, which the domain defined."""
        with self._sessions() as session:
            saga = session.get(Saga, saga_id)
            if saga is not None:
                saga.monto_total_centavos = int(monto)
                saga.cantidad = int(cantidad)
                saga.actualizada_en = ahora()

    def _registrar_compensacion(
        self, saga_id: str, nombre: str, resultado: str, detalle: str, orden: int
    ) -> None:
        """Persist one applied compensation."""
        momento = ahora()
        with self._sessions() as session:
            session.merge(
                CompensacionSaga(
                    saga_id=saga_id,
                    nombre=nombre,
                    resultado=resultado,
                    detalle=detalle[:512],
                    orden=orden,
                    finalizado_en=momento,
                )
            )
            saga = session.get(Saga, saga_id)
            if saga is not None:
                saga.actualizada_en = momento
            session.flush()

    def _cerrar_saga(self, saga_id: str, estado: str, error_texto: str = "") -> None:
        """Close the saga with its final state."""
        momento = ahora()
        with self._sessions() as session:
            saga = session.get(Saga, saga_id)
            if saga is None:  # pragma: no cover - la saga se creo al reclamar
                return
            saga.estado = estado
            saga.error = error_texto[:512]
            saga.actualizada_en = momento

    def _guardar_resultado(self, saga_id: str, huella: str, datos: dict) -> None:
        """Store the answer of the command in the idempotency table."""
        with self._sessions() as session:
            store = IdempotencyStore(session)
            store.save_result(saga_id, huella, datos)

    # ------------------------------------------------------------ lectura --
    def _respuesta(self, saga_id: str, idempotente: bool) -> dict | None:
        """The persisted state of a saga, or None when it does not exist."""
        with self._sessions() as session:
            saga = session.get(Saga, saga_id)
            if saga is None:
                return None
            return self._datos(session, saga, idempotente=idempotente)

    def _estado_persistido(self, saga_id: str) -> tuple[str, dict] | None:
        """The request hash and the answer of a saga, or None."""
        with self._sessions() as session:
            saga = session.get(Saga, saga_id)
            if saga is None:
                return None
            return saga.peticion_hash, self._datos(session, saga, idempotente=True)

    def _datos(self, session, saga: Saga, idempotente: bool) -> dict:
        """The saga row, its steps and its compensations as the contract dict."""
        pasos = (
            session.execute(
                select(PasoSaga).where(PasoSaga.saga_id == saga.saga_id).order_by(PasoSaga.orden)
            )
            .scalars()
            .all()
        )
        compensaciones = (
            session.execute(
                select(CompensacionSaga)
                .where(CompensacionSaga.saga_id == saga.saga_id)
                .order_by(CompensacionSaga.orden)
            )
            .scalars()
            .all()
        )
        return {
            "saga_id": saga.saga_id,
            "estado": saga.estado,
            "pasos": [
                {
                    "nombre": paso.nombre,
                    "resultado": paso.resultado,
                    "detalle": paso.detalle,
                    "iniciado_en": a_iso(paso.iniciado_en),
                    "finalizado_en": a_iso(paso.finalizado_en),
                }
                for paso in pasos
            ],
            "compensaciones": [
                {
                    "nombre": comp.nombre,
                    "resultado": comp.resultado,
                    "detalle": comp.detalle,
                    "finalizado_en": a_iso(comp.finalizado_en),
                }
                for comp in compensaciones
            ],
            "evento_id": saga.evento_id,
            "seccion_id": saga.seccion_id,
            "cantidad": int(saga.cantidad or 0),
            "cliente_id": saga.cliente_id,
            "error": saga.error or "",
            "actualizada_en": a_iso(saga.actualizada_en),
            "monto_total_centavos": int(saga.monto_total_centavos or 0),
            "moneda": saga.moneda,
            "idempotente": bool(idempotente),
        }

    # ----------------------------------------------------------- validacion --
    def _validar(
        self,
        saga_id: str,
        evento_id: str,
        seccion_id: str,
        butaca_ids: Iterable[str] | None,
        cliente_id: str,
        moneda: str | None,
    ) -> tuple[str, str, list[str], str, str]:
        """Check the request of the purchase and normalize its fields."""
        identificador = str(saga_id or "").strip()
        if not identificador:
            raise ServiceError(
                CODES.INVALID,
                "el saga_id es obligatorio",
                details={"saga_id": saga_id},
            )

        evento = str(evento_id or "").strip()
        if not evento:
            raise ServiceError(
                CODES.INVALID,
                "el evento es obligatorio",
                details={"saga_id": identificador},
            )

        seccion = str(seccion_id or "").strip()
        if not seccion:
            raise ServiceError(
                CODES.INVALID,
                "la seccion es obligatoria",
                details={"saga_id": identificador},
            )

        elegidas: list[str] = []
        for butaca_id in butaca_ids or []:
            valor = str(butaca_id or "").strip()
            if not valor:
                raise ServiceError(
                    CODES.INVALID,
                    "una butaca del pedido no tiene identificador",
                    details={"saga_id": identificador},
                )
            elegidas.append(valor)
        if not elegidas:
            raise ServiceError(
                CODES.INVALID,
                "una compra necesita al menos una butaca",
                details={"saga_id": identificador},
            )
        if len(set(elegidas)) != len(elegidas):
            raise ServiceError(
                CODES.INVALID,
                "una butaca aparece mas de una vez en la compra",
                details={"saga_id": identificador},
            )

        # La moneda se normaliza aqui; el monto no: lo define el dominio.
        return (
            evento,
            seccion,
            elegidas,
            str(cliente_id or "").strip(),
            str(moneda or MONEDA_POR_DEFECTO).strip().upper() or MONEDA_POR_DEFECTO,
        )
