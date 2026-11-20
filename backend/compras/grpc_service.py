"""gRPC adapter of the `compras` service.

The adapter translates the domain errors into a gRPC status plus a
machine readable code: the details of the status carry a JSON document
with the code of `common.errors`, so the gateway and the BFF can react to
`INVALID`, `NOT_FOUND`, `CONFLICT_IDEMPOTENCIA` or `DEPENDENCY_UNAVAILABLE`
without parsing free text. It is the mirror of `emision/grpc_service.py`.
"""

from __future__ import annotations

import json
import logging

import grpc

import compras_pb2
import compras_pb2_grpc
from common.errors import CODES, ServiceError

from .service import ComprasService

logger = logging.getLogger("compras.grpc")

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


def _a_proto_paso(datos: dict) -> compras_pb2.Paso:
    """Domain step -> protobuf step."""
    return compras_pb2.Paso(
        nombre=datos.get("nombre", "") or "",
        resultado=datos.get("resultado", "") or "",
        detalle=datos.get("detalle", "") or "",
        iniciado_en=datos.get("iniciado_en", "") or "",
        finalizado_en=datos.get("finalizado_en", "") or "",
    )


def _a_proto_compensacion(datos: dict) -> compras_pb2.Compensacion:
    """Domain compensation -> protobuf compensation."""
    return compras_pb2.Compensacion(
        nombre=datos.get("nombre", "") or "",
        resultado=datos.get("resultado", "") or "",
        detalle=datos.get("detalle", "") or "",
        finalizado_en=datos.get("finalizado_en", "") or "",
    )


def _a_proto_compra(datos: dict) -> compras_pb2.Compra:
    """Domain purchase -> protobuf purchase."""
    return compras_pb2.Compra(
        saga_id=datos.get("saga_id", "") or "",
        estado=datos.get("estado", "") or "",
        pasos=[_a_proto_paso(paso) for paso in datos.get("pasos") or []],
        compensaciones=[
            _a_proto_compensacion(comp) for comp in datos.get("compensaciones") or []
        ],
        evento_id=datos.get("evento_id", "") or "",
        seccion_id=datos.get("seccion_id", "") or "",
        cantidad=int(datos.get("cantidad", 0) or 0),
        cliente_id=datos.get("cliente_id", "") or "",
        error=datos.get("error", "") or "",
        actualizada_en=datos.get("actualizada_en", "") or "",
        monto_total_centavos=int(datos.get("monto_total_centavos", 0) or 0),
        moneda=datos.get("moneda", "") or "",
    )


def _a_proto_respuesta(resultado: dict) -> compras_pb2.CompraResponse:
    """Domain answer -> protobuf answer."""
    return compras_pb2.CompraResponse(
        compra=_a_proto_compra(resultado),
        idempotente=bool(resultado.get("idempotente", False)),
    )


async def _abortar(context, error: ServiceError):
    """Abort the call with the gRPC status and the machine readable code."""
    status = GRPC_STATUS_BY_CODE.get(error.code, grpc.StatusCode.UNKNOWN)
    detalles = json.dumps(error.to_dict(), ensure_ascii=False)
    logger.warning(
        "compras_error",
        extra={
            "event_fields": {
                "codigo": error.code,
                "mensaje": error.message,
                "grpc_status": status.name,
            }
        },
    )
    await context.abort(status, detalles)


class ComprasServicer(compras_pb2_grpc.ComprasServiceServicer):
    """Implementation of `compras.ComprasService`."""

    def __init__(self, servicio: ComprasService) -> None:
        self._servicio = servicio

    async def IniciarCompra(self, request, context):
        """Run the saga of the purchase (idempotent by `saga_id`)."""
        try:
            resultado = await self._servicio.iniciar_compra(
                saga_id=request.saga_id,
                evento_id=request.evento_id,
                seccion_id=request.seccion_id,
                butaca_ids=list(request.butaca_ids),
                cliente_id=request.cliente_id,
                moneda=request.moneda or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return compras_pb2.CompraResponse()
        return _a_proto_respuesta(resultado)

    async def ConsultarCompra(self, request, context):
        """The persisted state of a saga."""
        try:
            resultado = await self._servicio.consultar_compra(request.saga_id)
        except ServiceError as error:
            await _abortar(context, error)
            return compras_pb2.CompraResponse()
        return _a_proto_respuesta(resultado)


def registrar(server, servicer: ComprasServicer) -> None:
    """Attach the servicer to the gRPC server."""
    compras_pb2_grpc.add_ComprasServiceServicer_to_server(servicer, server)
