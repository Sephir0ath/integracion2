"""Clientes gRPC de las dependencias de la saga.

El orquestador no habla HTTP ni conoce los modelos de los otros servicios:
habla con tres clientes finos, uno por dependencia, que envuelven la unica
salida del sistema (`common.grpc.unary`) y devuelven diccionarios planos,
igual que los devuelve el dominio de cada servicio.

Dos decisiones que no son de estilo:

* **El stub se crea dentro del event loop.** Un canal de `grpc.aio`
  pertenece al loop que lo abre; crearlo al construir el cliente lo ataria
  al loop equivocado cuando el cliente se construye fuera del servidor.
* **El fallo de la dependencia se traduce a `ServiceError`.** Los servicios
  de dominio meten `error.to_dict()` en los detalles del status gRPC, asi
  que aqui se lee el `code` de ese JSON; cuando los detalles no son JSON se
  mapea el status gRPC a un codigo de `common.errors.CODES`. El orquestador
  no parsea texto libre ni confunde un fallo con una confirmacion.

Toda llamada lleva el limite de tiempo de la configuracion
(`GRPC_DEADLINE`): ninguna dependencia lenta deja la saga esperando.
"""

from __future__ import annotations

import asyncio
import json
import logging

import grpc

import butacas_pb2
import butacas_pb2_grpc
import emision_pb2
import emision_pb2_grpc
import pagos_pb2
import pagos_pb2_grpc

from common.config import Settings
from common.errors import CODES, ServiceError
from common.grpc import create_channel, unary

logger = logging.getLogger("compras.clients")

# Codigo de `common.errors` asociado a cada status gRPC. Solo se usa cuando
# los detalles del status no traen el JSON con el codigo del dominio.
CODIGO_POR_STATUS: dict[grpc.StatusCode, str] = {
    grpc.StatusCode.NOT_FOUND: CODES.NOT_FOUND,
    grpc.StatusCode.ALREADY_EXISTS: CODES.ALREADY_EXISTS,
    grpc.StatusCode.INVALID_ARGUMENT: CODES.INVALID,
    grpc.StatusCode.FAILED_PRECONDITION: CODES.PAYMENT_REQUIRED,
    grpc.StatusCode.UNAVAILABLE: CODES.DEPENDENCY_UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED: CODES.DEPENDENCY_UNAVAILABLE,
    grpc.StatusCode.RESOURCE_EXHAUSTED: CODES.BULKHEAD_SATURATED,
    grpc.StatusCode.ABORTED: CODES.CONFLICT_IDEMPOTENCIA,
}


def traducir_error(error: grpc.aio.AioRpcError, servicio: str) -> ServiceError:
    """Translate the failure of a dependency into a domain error.

    The domain services abort the call with a gRPC status and a JSON
    document in the details (`error.to_dict()`). That JSON is the exact
    failure: it keeps the machine readable code, so the saga can react to
    `ALREADY_EXISTS` or `CIRCUIT_OPEN` without parsing free text. A status
    with no code in its details falls back to the gRPC status mapping.
    """
    detalles = error.details() or ""
    codigo_grpc = error.code()
    logger.warning(
        "compras_dependencia_error",
        extra={
            "event_fields": {
                "servicio": servicio,
                "grpc_status": codigo_grpc.name,
                "detalle": detalles,
            }
        },
    )

    try:
        contenido = json.loads(detalles)
    except (TypeError, ValueError):
        contenido = None

    if isinstance(contenido, dict) and contenido.get("code"):
        extra = contenido.get("details")
        return ServiceError(
            str(contenido["code"]),
            str(contenido.get("message") or f"el servicio de {servicio} fallo"),
            details={**(extra if isinstance(extra, dict) else {}), "servicio": servicio},
        )

    codigo = CODIGO_POR_STATUS.get(codigo_grpc, CODES.DEPENDENCY_UNAVAILABLE)
    return ServiceError(
        codigo,
        f"el servicio de {servicio} no esta disponible ({codigo_grpc.name})",
        details={"servicio": servicio, "grpc_status": codigo_grpc.name},
    )


class _ClienteDependencia:
    """Comportamiento comun de los tres clientes: canal, plazo y traduccion."""

    # Nombre de la dependencia en los errores y en los logs.
    servicio = "dependencia"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        self._target = ""
        self._deadline = float(settings.grpc_deadline)
        self._channel = channel
        self._stub = stub

    # ------------------------------------------------------------ estado --
    @property
    def deadline(self) -> float:
        """Time limit of every call to the dependency."""
        return self._deadline

    @property
    def target(self) -> str:
        """Address of the dependency."""
        return self._target

    # ------------------------------------------------------------- canal --
    def _obtener_stub(self):
        """The stub of the dependency, opening the channel when it is needed.

        The channel is opened in the event loop that makes the call: a
        channel of `grpc.aio` belongs to the loop that created it.
        """
        if self._stub is None:
            if self._channel is None:
                self._channel = create_channel(self._target)
            self._stub = self._crear_stub(self._channel)
        return self._stub

    def _crear_stub(self, channel):
        """The stub class of the dependency (implemented by every client)."""
        raise NotImplementedError

    async def _llamar(self, metodo: str, peticion):
        """Make one unary call under the deadline of the configuration."""
        stub = self._obtener_stub()
        try:
            return await unary(
                self._channel,
                getattr(stub, metodo),
                peticion,
                timeout=self._deadline,
            )
        except grpc.aio.AioRpcError as error:
            raise traducir_error(error, self.servicio) from error
        except asyncio.TimeoutError as error:
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"el servicio de {self.servicio} no respondio dentro del limite de {self._deadline}s",
                details={"servicio": self.servicio, "deadline": self._deadline},
            ) from error

    async def close(self) -> None:
        """Close the channel of the dependency."""
        if self._channel is not None:
            await self._channel.close()


class ButacasClient(_ClienteDependencia):
    """Cliente del servicio `butacas`: inventario y reservas."""

    servicio = "butacas"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.butacas_target

    def _crear_stub(self, channel):
        return butacas_pb2_grpc.ButacasServiceStub(channel)

    async def reservar(
        self,
        saga_id: str,
        evento_id: str,
        seccion_id: str,
        butaca_ids: list[str],
        idempotency_key: str | None = None,
    ) -> dict:
        """Reserve the exact seats of the purchase (saga step `RESERVAR`)."""
        peticion = butacas_pb2.ReservarRequest(
            saga_id=saga_id,
            evento_id=evento_id,
            seccion_id=seccion_id,
            butaca_ids=list(butaca_ids or []),
            idempotency_key=idempotency_key or "",
        )
        respuesta = await self._llamar("Reservar", peticion)
        reserva = respuesta.reserva
        return {
            "reserva": {
                "reserva_id": reserva.reserva_id,
                "saga_id": reserva.saga_id or saga_id,
                "seccion_id": reserva.seccion_id or seccion_id,
                "estado": reserva.estado,
                "precio_centavos": int(reserva.precio_centavos),
                "creada_en": reserva.creada_en,
                "butacas": [_butaca_como_datos(butaca) for butaca in reserva.butacas],
            },
            "butacas": [_butaca_como_datos(butaca) for butaca in respuesta.butacas],
            "estado": respuesta.estado,
        }

    async def liberar(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Release the seats of a saga (compensation `LIBERAR`)."""
        peticion = butacas_pb2.LiberarRequest(
            saga_id=saga_id,
            idempotency_key=idempotency_key or "",
        )
        respuesta = await self._llamar("Liberar", peticion)
        return {
            "liberado": bool(respuesta.liberado),
            "butacas_liberadas": int(respuesta.butacas_liberadas),
            "estado": respuesta.estado,
        }

    async def confirmar_venta(self, saga_id: str) -> dict:
        """Close the sale of the seats once the saga succeeded."""
        peticion = butacas_pb2.ConfirmarVentaRequest(saga_id=saga_id)
        respuesta = await self._llamar("ConfirmarVenta", peticion)
        return {
            "confirmada": bool(respuesta.confirmada),
            "butacas_confirmadas": int(respuesta.butacas_confirmadas),
            "estado": respuesta.estado,
        }


class PagosClient(_ClienteDependencia):
    """Cliente del servicio `pagos`: cobro y reembolso."""

    servicio = "pagos"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.pagos_target

    def _crear_stub(self, channel):
        return pagos_pb2_grpc.PagosServiceStub(channel)

    async def cobrar(
        self,
        saga_id: str,
        monto_centavos: int,
        moneda: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """Charge a saga (saga step `COBRAR`)."""
        peticion = pagos_pb2.CobrarRequest(
            saga_id=saga_id,
            monto_centavos=int(monto_centavos),
            moneda=moneda,
            idempotency_key=idempotency_key or "",
        )
        respuesta = await self._llamar("Cobrar", peticion)
        return _pago_como_datos(respuesta)

    async def reembolsar(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Refund the payment of a saga (compensation `REEMBOLSAR`)."""
        peticion = pagos_pb2.ReembolsarRequest(
            saga_id=saga_id,
            idempotency_key=idempotency_key or "",
        )
        respuesta = await self._llamar("Reembolsar", peticion)
        return _pago_como_datos(respuesta)


class EmisionClient(_ClienteDependencia):
    """Cliente del servicio `emision`: emision y anulacion de entradas."""

    servicio = "emision"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.emision_target

    def _crear_stub(self, channel):
        return emision_pb2_grpc.EmisionServiceStub(channel)

    async def emitir(
        self,
        saga_id: str,
        pago_id: str,
        butacas: list[dict],
        idempotency_key: str | None = None,
    ) -> dict:
        """Issue one ticket per seat (saga step `EMITIR`)."""
        peticion = emision_pb2.EmitirRequest(
            saga_id=saga_id,
            pago_id=pago_id,
            butacas=[
                emision_pb2.ButacaRef(
                    butaca_id=butaca.get("butaca_id", "") or "",
                    seccion_id=butaca.get("seccion_id", "") or "",
                    fila=butaca.get("fila", "") or "",
                    numero=int(butaca.get("numero") or 0),
                )
                for butaca in butacas or []
            ],
            idempotency_key=idempotency_key or "",
        )
        respuesta = await self._llamar("Emitir", peticion)
        return _entradas_como_datos(respuesta)

    async def anular(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        """Void the tickets of a saga (compensation `ANULAR`)."""
        peticion = emision_pb2.AnularRequest(
            saga_id=saga_id,
            idempotency_key=idempotency_key or "",
        )
        respuesta = await self._llamar("Anular", peticion)
        return _entradas_como_datos(respuesta)


# ------------------------------------------------------------- auxiliares --
def _butaca_como_datos(butaca) -> dict:
    """Protobuf seat -> dict, the shape the domain of `butacas` uses."""
    return {
        "butaca_id": butaca.butaca_id,
        "seccion_id": butaca.seccion_id,
        "fila": butaca.fila,
        "numero": int(butaca.numero),
        "estado": butaca.estado,
        "saga_id": butaca.saga_id,
    }


def _pago_como_datos(respuesta) -> dict:
    """Protobuf payment answer -> dict."""
    pago = respuesta.pago
    return {
        "pago": {
            "pago_id": pago.pago_id,
            "saga_id": pago.saga_id,
            "monto_centavos": int(pago.monto_centavos),
            "moneda": pago.moneda,
            "estado": pago.estado,
            "pasarela_ref": pago.pasarela_ref,
            "actualizado_en": pago.actualizado_en,
        },
        "estado": respuesta.estado,
        "idempotente": bool(respuesta.idempotente),
    }


def _entradas_como_datos(respuesta) -> dict:
    """Protobuf ticket answer -> dict."""
    return {
        "entradas": [
            {
                "entrada_id": entrada.entrada_id,
                "saga_id": entrada.saga_id,
                "butaca_id": entrada.butaca_id,
                "fila": entrada.fila,
                "numero": int(entrada.numero),
                "qr": entrada.qr,
                "estado": entrada.estado,
            }
            for entrada in respuesta.entradas
        ],
        "estado": respuesta.estado,
        "idempotente": bool(respuesta.idempotente),
    }
