"""gRPC adapter of the `butacas` service.

The adapter translates the domain errors into a gRPC status plus a machine
readable code: the details of the status carry a JSON document with the
code of `common.errors`, so the saga orchestrator can react to
`ALREADY_EXISTS`, `NOT_FOUND`, `INVALID` or `CONFLICT_IDEMPOTENCIA`
without parsing free text.
"""

from __future__ import annotations

import json
import logging

import grpc

import butacas_pb2
import butacas_pb2_grpc
from common.errors import CODES, ServiceError

from .service import ButacasService

logger = logging.getLogger("butacas.grpc")

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


def _a_proto_evento(datos: dict) -> butacas_pb2.Evento:
    """Domain event -> protobuf event."""
    return butacas_pb2.Evento(
        evento_id=datos.get("evento_id", ""),
        nombre=datos.get("nombre", ""),
        lugar=datos.get("lugar", "") or "",
        fecha=datos.get("fecha", "") or "",
    )


def _a_proto_seccion(datos: dict) -> butacas_pb2.Seccion:
    """Domain section -> protobuf section."""
    return butacas_pb2.Seccion(
        seccion_id=datos.get("seccion_id", ""),
        evento_id=datos.get("evento_id", ""),
        nombre=datos.get("nombre", ""),
        capacidad_total=int(datos.get("capacidad_total", 0)),
        cantidad_disponible=int(datos.get("cantidad_disponible", 0)),
        precio_centavos=int(datos.get("precio_centavos", 0)),
    )


def _a_proto_butaca(datos: dict) -> butacas_pb2.Butaca:
    """Domain seat -> protobuf seat."""
    return butacas_pb2.Butaca(
        butaca_id=datos.get("butaca_id", ""),
        seccion_id=datos.get("seccion_id", ""),
        fila=datos.get("fila", ""),
        numero=int(datos.get("numero", 0)),
        estado=datos.get("estado", ""),
        saga_id=datos.get("saga_id", "") or "",
    )


def _a_proto_butacas(datos: list[dict]) -> list:
    """Domain seats -> protobuf seats."""
    return [_a_proto_butaca(butaca) for butaca in datos]


def _a_proto_reserva(datos: dict | None) -> butacas_pb2.Reserva:
    """Domain reservation -> protobuf reservation."""
    datos = datos or {}
    return butacas_pb2.Reserva(
        reserva_id=datos.get("reserva_id", ""),
        saga_id=datos.get("saga_id", ""),
        seccion_id=datos.get("seccion_id", ""),
        butacas=_a_proto_butacas(datos.get("butacas", [])),
        estado=datos.get("estado", ""),
        creada_en=datos.get("creada_en", "") or "",
        precio_centavos=int(datos.get("precio_centavos", 0)),
    )


async def _abortar(context, error: ServiceError):
    """Abort the call with the gRPC status and the machine readable code."""
    status = GRPC_STATUS_BY_CODE.get(error.code, grpc.StatusCode.UNKNOWN)
    detalles = json.dumps(error.to_dict(), ensure_ascii=False)
    logger.warning(
        "butacas_error",
        extra={
            "event_fields": {
                "codigo": error.code,
                "mensaje": error.message,
                "grpc_status": status.name,
            }
        },
    )
    await context.abort(status, detalles)


class ButacasServicer(butacas_pb2_grpc.ButacasServiceServicer):
    """Implementation of `butacas.ButacasService`."""

    def __init__(self, servicio: ButacasService) -> None:
        self._servicio = servicio

    # ------------------------------------------------------------ consulta --
    async def ListarEventos(self, request, context):
        """The events of the venue."""
        try:
            eventos = self._servicio.listar_eventos()
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.ListarEventosResponse()
        return butacas_pb2.ListarEventosResponse(eventos=[_a_proto_evento(e) for e in eventos])

    async def ListarSecciones(self, request, context):
        """The sections of an event, with the free seats counted from the rows."""
        try:
            secciones = self._servicio.listar_secciones(request.evento_id or None)
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.ListarSeccionesResponse()
        return butacas_pb2.ListarSeccionesResponse(
            secciones=[_a_proto_seccion(s) for s in secciones]
        )

    async def ListarButacas(self, request, context):
        """The seating plan of a section."""
        try:
            butacas = self._servicio.listar_butacas(request.seccion_id)
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.ListarButacasResponse()
        return butacas_pb2.ListarButacasResponse(butacas=_a_proto_butacas(butacas))

    # ------------------------------------------------------------- saga --
    async def Reservar(self, request, context):
        """Reserve the chosen seats (saga step `RESERVAR`)."""
        try:
            resultado = self._servicio.reservar(
                saga_id=request.saga_id,
                evento_id=request.evento_id,
                seccion_id=request.seccion_id,
                butaca_ids=list(request.butaca_ids),
                idempotency_key=request.idempotency_key or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.ReservaResponse()
        return butacas_pb2.ReservaResponse(
            reserva=_a_proto_reserva(resultado["reserva"]),
            butacas=_a_proto_butacas(resultado["butacas"]),
            estado=resultado["estado"],
        )

    async def Liberar(self, request, context):
        """Release the seats of a saga (compensation `LIBERAR`)."""
        try:
            resultado = self._servicio.liberar(
                saga_id=request.saga_id,
                idempotency_key=request.idempotency_key or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.LiberarResponse()
        return butacas_pb2.LiberarResponse(
            liberado=bool(resultado["liberado"]),
            butacas_liberadas=int(resultado["butacas_liberadas"]),
            estado=resultado["estado"],
        )

    async def ConfirmarVenta(self, request, context):
        """Close the sale of the seats that the saga holds."""
        try:
            resultado = self._servicio.confirmar_venta(saga_id=request.saga_id)
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.ConfirmarVentaResponse()
        return butacas_pb2.ConfirmarVentaResponse(
            confirmada=bool(resultado["confirmada"]),
            butacas_confirmadas=int(resultado["butacas_confirmadas"]),
            estado=resultado["estado"],
        )

    async def ConsultarReserva(self, request, context):
        """The reservation of a saga and its seats."""
        try:
            resultado = self._servicio.consultar_reserva(request.saga_id)
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.ReservaResponse()
        return butacas_pb2.ReservaResponse(
            reserva=_a_proto_reserva(resultado["reserva"]),
            butacas=_a_proto_butacas(resultado["butacas"]),
            estado=resultado["estado"],
        )

    # -------------------------------------------------------------- crud --
    async def CrearEvento(self, request, context):
        """Create an event."""
        try:
            evento = self._servicio.crear_evento(
                nombre=request.nombre, lugar=request.lugar, fecha=request.fecha
            )
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.Evento()
        return _a_proto_evento(evento)

    async def ActualizarEvento(self, request, context):
        """Update an event."""
        try:
            evento = self._servicio.actualizar_evento(
                evento_id=request.evento_id,
                nombre=request.nombre,
                lugar=request.lugar,
                fecha=request.fecha,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.Evento()
        return _a_proto_evento(evento)

    async def EliminarEvento(self, request, context):
        """Delete an event that was not sold."""
        try:
            resultado = self._servicio.eliminar_evento(request.evento_id)
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.EliminarResponse()
        return butacas_pb2.EliminarResponse(eliminado=bool(resultado["eliminado"]))

    async def CrearSeccion(self, request, context):
        """Create a section and the seats of its seating plan."""
        try:
            seccion = self._servicio.crear_seccion(
                evento_id=request.evento_id,
                nombre=request.nombre,
                capacidad_total=request.capacidad_total,
                precio_centavos=request.precio_centavos,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.Seccion()
        return _a_proto_seccion(seccion)

    async def ActualizarSeccion(self, request, context):
        """Update a section and adjust its seating plan."""
        try:
            seccion = self._servicio.actualizar_seccion(
                seccion_id=request.seccion_id,
                nombre=request.nombre,
                capacidad_total=request.capacidad_total or None,
                precio_centavos=request.precio_centavos or None,
            )
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.Seccion()
        return _a_proto_seccion(seccion)

    async def EliminarSeccion(self, request, context):
        """Delete a section that was not sold."""
        try:
            resultado = self._servicio.eliminar_seccion(request.seccion_id)
        except ServiceError as error:
            await _abortar(context, error)
            return butacas_pb2.EliminarResponse()
        return butacas_pb2.EliminarResponse(eliminado=bool(resultado["eliminado"]))


def registrar(server, servicer: ButacasServicer) -> None:
    """Attach the servicer to the gRPC server."""
    butacas_pb2_grpc.add_ButacasServiceServicer_to_server(servicer, server)
