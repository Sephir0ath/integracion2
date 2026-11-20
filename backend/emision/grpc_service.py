"""gRPC adapter of the `emision` service.

The adapter translates the domain errors into a gRPC status plus a
machine readable code: the details of the status carry a JSON document
with the code of `common.errors`, so the saga orchestrator can react to
`PAYMENT_REQUIRED`, `DEPENDENCY_UNAVAILABLE`, `INVALID` or
`CONFLICT_IDEMPOTENCIA` without parsing free text.
"""

from __future__ import annotations

import json
import logging

import grpc

import emision_pb2
import emision_pb2_grpc
from common.errors import CODES, ServiceError

from .service import EmisionService

logger = logging.getLogger("emision.grpc")

# gRPC status associated with every domain code.
GRPC_STATUS_BY_CODE: dict[str, grpc.StatusCode] = {
    CODES.NOT_FOUND: grpc.StatusCode.NOT_FOUND,
    CODES.ALREADY_EXISTS: grpc.StatusCode.ALREADY_EXISTS,
    CODES.INVALID: grpc.StatusCode.INVALID_ARGUMENT,
    CODES.PAYMENT_REQUIRED: grpc.StatusCode.FAILED_PRECONDITION,
    CODES.DEPENDENCY_UNAVAILABLE: grpc.StatusCode.UNAVAILABLE,
    CODES.BULKHEAD_SATURATED: grpc.StatusCode.RESOURCE_EXHAUSTED,
    CODES.CIRCUIT_OPEN: grpc.StatusCode.UNAVAILABLE,
    CODES.CONFLICT_IDEMPOTENCIA: grpc.StatusCode.ABORTED,
}


def _a_proto_entrada(datos: dict) -> emision_pb2.Entrada:
    """Domain ticket -> protobuf ticket."""
    return emision_pb2.Entrada(
        entrada_id=datos.get("entrada_id", ""),
        saga_id=datos.get("saga_id", ""),
        butaca_id=datos.get("butaca_id", ""),
        fila=datos.get("fila", "") or "",
        numero=int(datos.get("numero", 0)),
        qr=datos.get("qr", "") or "",
        estado=datos.get("estado", "") or "",
    )


def _a_proto_respuesta(resultado: dict) -> emision_pb2.EntradasResponse:
    """Domain answer -> protobuf answer."""
    return emision_pb2.EntradasResponse(
        entradas=[_a_proto_entrada(entrada) for entrada in resultado.get("entradas", [])],
        estado=resultado.get("estado", "") or "",
        idempotente=bool(resultado.get("idempotente", False)),
    )


async def _abortar(context, error: ServiceError):
    """Abort the call with the gRPC status and the machine readable code."""
    status = GRPC_STATUS_BY_CODE.get(error.code, grpc.StatusCode.UNKNOWN)
    detalles = json.dumps(error.to_dict(), ensure_ascii=False)
    logger.warning(
        "emision_error",
        extra={
            "event_fields": {
                "codigo": error.code,
                "mensaje": error.message,
                "grpc_status": status.name,
            }
        },
    )
    await context.abort(status, detalles)


class EmisionServicer(emision_pb2_grpc.EmisionServiceServicer):
    """Implementation of `emision.EmisionService`."""

    def __init__(self, servicio: EmisionService) -> None:
        self._servicio = servicio

    async def Emitir(self, request, context):
        """Issue one ticket per seat (saga step `EMITIR`)."""
        try:
            resultado = await self._servicio.emitir(
                saga_id=request.saga_id,
                pago_id=request.pago_id,
                butacas=[
                    {
                        "butaca_id": butaca.butaca_id,
                        "seccion_id": butaca.seccion_id,
                        "fila": butaca.fila,
                        "numero": butaca.numero,
                    }
                    for butaca in request.butacas
                ],
                idempotency_key=request.idempotency_key or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return emision_pb2.EntradasResponse()
        return _a_proto_respuesta(resultado)

    async def Anular(self, request, context):
        """Void the tickets of a saga (compensation `ANULAR`)."""
        try:
            resultado = await self._servicio.anular(
                saga_id=request.saga_id,
                idempotency_key=request.idempotency_key or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return emision_pb2.EntradasResponse()
        return _a_proto_respuesta(resultado)

    async def Listar(self, request, context):
        """The tickets of a saga, with their state."""
        try:
            resultado = self._servicio.listar(request.saga_id)
        except ServiceError as error:
            await _abortar(context, error)
            return emision_pb2.EntradasResponse()
        return _a_proto_respuesta(resultado)


def registrar(server, servicer: EmisionServicer) -> None:
    """Attach the servicer to the gRPC server."""
    emision_pb2_grpc.add_EmisionServiceServicer_to_server(servicer, server)
