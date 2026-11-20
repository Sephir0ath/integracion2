"""Client of the `pagos` service.

This is the only outbound call of the `emision` service and it is where
the requirement of the system becomes real: **every call between
services has a time limit**. The deadline comes from the configuration
(`GRPC_DEADLINE`) and it is applied to every call, so a slow `pagos`
never holds the emission forever.

The failures of the dependency are translated into domain errors, and
they are never mistaken for a confirmation:

* the transport fails (unavailable, deadline exceeded, timeout) ->
  `DEPENDENCY_UNAVAILABLE`;
* the saga has no payment -> `NOT_FOUND`, which the domain turns into
  "there is no confirmed payment" (`PAYMENT_REQUIRED`).
"""

from __future__ import annotations

import asyncio
import logging

import grpc
import pagos_pb2
import pagos_pb2_grpc

from common.config import Settings
from common.errors import CODES, ServiceError
from common.grpc import create_channel, unary

logger = logging.getLogger("emision.pagos")


class PagosClient:
    """Read the payment of a saga from the `pagos` service."""

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        self._target = settings.pagos_target
        self._deadline = float(settings.grpc_deadline)
        self._channel = channel
        self._stub = stub

    def _obtener_stub(self):
        """The stub of `pagos`, opening the channel when it is needed.

        The channel is opened in the event loop that makes the call: a
        channel of `grpc.aio` belongs to the loop that created it, so
        opening it outside the loop of the server would bind the calls to
        the wrong loop.
        """
        if self._stub is None:
            if self._channel is None:
                self._channel = create_channel(self._target)
            self._stub = pagos_pb2_grpc.PagosServiceStub(self._channel)
        return self._stub

    @property
    def deadline(self) -> float:
        """Time limit of every call to `pagos`."""
        return self._deadline

    @property
    def target(self) -> str:
        """Address of the `pagos` service."""
        return self._target

    async def consultar_pago(self, saga_id: str) -> dict:
        """The payment of a saga, as the ledger of `pagos` sees it."""
        peticion = pagos_pb2.ConsultarPagoRequest(saga_id=saga_id)
        stub = self._obtener_stub()
        try:
            respuesta = await unary(
                self._channel,
                stub.ConsultarPago,
                peticion,
                timeout=self._deadline,
            )
        except grpc.aio.AioRpcError as error:
            raise self._traducir(error, saga_id) from error
        except asyncio.TimeoutError as error:
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"el servicio de pagos no respondio dentro del limite de {self._deadline}s",
                details={"saga_id": saga_id, "deadline": self._deadline},
            ) from error

        pago = respuesta.pago
        return {
            "pago_id": pago.pago_id or "",
            "saga_id": pago.saga_id or saga_id,
            "estado": (respuesta.estado or pago.estado or "").upper(),
            "monto_centavos": int(pago.monto_centavos),
        }

    def _traducir(self, error: grpc.aio.AioRpcError, saga_id: str) -> ServiceError:
        """Translate the failure of the dependency into a domain error."""
        codigo = error.code()
        logger.warning(
            "emision_pagos_error",
            extra={
                "event_fields": {
                    "saga_id": saga_id,
                    "grpc_status": codigo.name,
                    "detalle": error.details(),
                }
            },
        )
        if codigo == grpc.StatusCode.NOT_FOUND:
            return ServiceError(
                CODES.NOT_FOUND,
                f"el servicio de pagos no tiene un pago para la saga '{saga_id}'",
                details={"saga_id": saga_id},
            )
        return ServiceError(
            CODES.DEPENDENCY_UNAVAILABLE,
            f"el servicio de pagos no esta disponible ({codigo.name})",
            details={"saga_id": saga_id, "grpc_status": codigo.name},
        )

    async def close(self) -> None:
        """Close the channel of the dependency."""
        if self._channel is not None:
            await self._channel.close()
