"""gRPC adapter of the `pagos` service.

The adapter translates the domain errors into a gRPC status plus a machine
readable code: the details of the status carry a JSON document with the
code of `common.errors`, so the saga orchestrator can react to
`CIRCUIT_OPEN`, `BULKHEAD_SATURATED`, `DEPENDENCY_UNAVAILABLE` or
`CONFLICT_IDEMPOTENCIA` without parsing free text.
"""

from __future__ import annotations

import json
import logging

import grpc

import pagos_pb2
import pagos_pb2_grpc
from common.errors import CODES, ServiceError

from .service import PagosService

logger = logging.getLogger("pagos.grpc")

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


def _a_proto_pago(datos: dict) -> pagos_pb2.Pago:
    """Domain payment -> protobuf payment."""
    return pagos_pb2.Pago(
        pago_id=datos.get("pago_id", ""),
        saga_id=datos.get("saga_id", ""),
        monto_centavos=int(datos.get("monto_centavos", 0)),
        moneda=datos.get("moneda", ""),
        estado=datos.get("estado", ""),
        pasarela_ref=datos.get("pasarela_ref", "") or "",
        actualizado_en=datos.get("actualizado_en", "") or "",
    )


def _a_proto_respuesta(resultado: dict) -> pagos_pb2.PagoResponse:
    """Domain answer -> protobuf answer."""
    pago = resultado.get("pago") or {}
    return pagos_pb2.PagoResponse(
        pago=_a_proto_pago(pago),
        estado=pago.get("estado", ""),
        idempotente=bool(resultado.get("idempotente", False)),
    )


async def _abortar(context, error: ServiceError):
    """Abort the call with the gRPC status and the machine readable code."""
    status = GRPC_STATUS_BY_CODE.get(error.code, grpc.StatusCode.UNKNOWN)
    detalles = json.dumps(error.to_dict(), ensure_ascii=False)
    logger.warning(
        "pagos_error",
        extra={
            "event_fields": {
                "codigo": error.code,
                "mensaje": error.message,
                "grpc_status": status.name,
            }
        },
    )
    await context.abort(status, detalles)


class PagosServicer(pagos_pb2_grpc.PagosServiceServicer):
    """Implementation of `pagos.PagosService`."""

    def __init__(self, servicio: PagosService) -> None:
        self._servicio = servicio

    async def ListarMetodosPago(self, request, context):
        """Payment methods accepted by the service."""
        metodos = [pagos_pb2.MetodoPago(metodo=m["metodo"], descripcion=m["descripcion"]) for m in self._servicio.listar_metodos_pago()]
        return pagos_pb2.ListarMetodosPagoResponse(metodos=metodos)

    async def Cobrar(self, request, context):
        """Charge a saga (saga step `COBRAR`)."""
        try:
            resultado = await self._servicio.cobrar(
                saga_id=request.saga_id,
                monto_centavos=request.monto_centavos,
                moneda=request.moneda or None,
                idempotency_key=request.idempotency_key or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return pagos_pb2.PagoResponse()
        return _a_proto_respuesta(resultado)

    async def Reembolsar(self, request, context):
        """Refund the payment of a saga (compensation `REEMBOLSAR`)."""
        try:
            resultado = await self._servicio.reembolsar(
                saga_id=request.saga_id,
                idempotency_key=request.idempotency_key or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return pagos_pb2.PagoResponse()
        return _a_proto_respuesta(resultado)

    async def ConsultarPago(self, request, context):
        """Read the payment of a saga."""
        try:
            datos = self._servicio.consultar_pago(request.saga_id)
        except ServiceError as error:
            await _abortar(context, error)
            return pagos_pb2.PagoResponse()
        return _a_proto_respuesta({"pago": datos, "idempotente": False})

    async def EstadoCircuito(self, request, context):
        """Live state of the circuit breaker and the bulkhead."""
        estado = self._servicio.estado_circuito()
        return pagos_pb2.EstadoCircuitoResponse(
            estado=estado.get("estado", ""),
            fallos_consecutivos=int(estado.get("fallos_consecutivos", 0)),
            llamadas_rechazadas=int(estado.get("llamadas_rechazadas", 0)),
            llamadas_en_curso=int(estado.get("llamadas_en_curso", 0)),
            rechazos_bulkhead=int(estado.get("rechazos_bulkhead", 0)),
            fallback=estado.get("fallback", ""),
        )


def registrar(server, servicer: PagosServicer) -> None:
    """Attach the servicer to the gRPC server."""
    pagos_pb2_grpc.add_PagosServiceServicer_to_server(servicer, server)
