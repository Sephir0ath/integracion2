"""Dobles de las dependencias externas del BFF.

Los dobles tienen los mismos metodos que los servicios reales y **registran
el limite de tiempo y las metadatos que reciben**, para que la prueba pueda
comprobar que toda llamada gRPC lleva su deadline y que la identidad del
cliente viaja al servicio interno.

Con `error` el doble falla como falla una dependencia caida: eso es lo que
ejercita la degradacion elegante del BFF (una dependencia opcional que se
cae) y el 503 con codigo de `common.errors` (una esencial).
"""

from __future__ import annotations

import grpc

import butacas_pb2
import compras_pb2
import emision_pb2
import pagos_pb2


def error_grpc(
    codigo: grpc.StatusCode = grpc.StatusCode.UNAVAILABLE,
    detalle: str = "el servicio no esta disponible",
) -> grpc.aio.AioRpcError:
    """El error de una dependencia que no puede responder."""
    return grpc.aio.AioRpcError(codigo, None, None, details=detalle)


class _StubBase:
    """Comportamiento comun de los dobles: registro de llamadas y error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def _grabar(self, metodo: str, request, timeout, metadata) -> None:
        self.calls.append(
            {
                "metodo": metodo,
                "timeout": timeout,
                "metadata": {
                    str(clave).lower(): valor for clave, valor in dict(metadata or {}).items()
                },
                "request": request,
            }
        )
        if self.error is not None:
            raise self.error

    @property
    def ultima_llamada(self) -> dict:
        """La ultima llamada registrada."""
        return self.calls[-1]


class FakeButacasStub(_StubBase):
    """Doble de `butacas.ButacasService`."""

    async def ListarEventos(self, request, timeout=None, metadata=None):
        """Los eventos del recinto."""
        self._grabar("ListarEventos", request, timeout, metadata)
        return butacas_pb2.ListarEventosResponse(
            eventos=[
                butacas_pb2.Evento(
                    evento_id="ev-1",
                    nombre="Partido decisivo",
                    lugar="El Campin",
                    fecha="2026-03-01T20:00:00",
                ),
                butacas_pb2.Evento(
                    evento_id="ev-2",
                    nombre="Concierto sinfonico",
                    lugar="Coliseo",
                    fecha="2026-04-01T20:00:00",
                ),
            ]
        )

    async def ListarSecciones(self, request, timeout=None, metadata=None):
        """Las secciones del recinto."""
        self._grabar("ListarSecciones", request, timeout, metadata)
        return butacas_pb2.ListarSeccionesResponse(
            secciones=[
                butacas_pb2.Seccion(
                    seccion_id="sec-1",
                    evento_id="ev-1",
                    nombre="Tribuna norte",
                    capacidad_total=32,
                    cantidad_disponible=30,
                    precio_centavos=55000,
                ),
                butacas_pb2.Seccion(
                    seccion_id="sec-2",
                    evento_id="ev-1",
                    nombre="Platea",
                    capacidad_total=48,
                    cantidad_disponible=48,
                    precio_centavos=120000,
                ),
            ]
        )

    async def ListarButacas(self, request, timeout=None, metadata=None):
        """El plano de la sala."""
        self._grabar("ListarButacas", request, timeout, metadata)
        return butacas_pb2.ListarButacasResponse(
            butacas=[
                butacas_pb2.Butaca(
                    butaca_id="sec-1-A1",
                    seccion_id=request.seccion_id,
                    fila="A",
                    numero=1,
                    estado="LIBRE",
                ),
                butacas_pb2.Butaca(
                    butaca_id="sec-1-A2",
                    seccion_id=request.seccion_id,
                    fila="A",
                    numero=2,
                    estado="RESERVADA",
                    saga_id="saga-1",
                ),
            ]
        )

    async def ConsultarReserva(self, request, timeout=None, metadata=None):
        """La reserva de una saga."""
        self._grabar("ConsultarReserva", request, timeout, metadata)
        butacas = [
            butacas_pb2.Butaca(
                butaca_id="sec-1-A1",
                seccion_id="sec-1",
                fila="A",
                numero=1,
                estado="RESERVADA",
                saga_id=request.saga_id,
            )
        ]
        reserva = butacas_pb2.Reserva(
            reserva_id="res-1",
            saga_id=request.saga_id,
            seccion_id="sec-1",
            estado="ACTIVA",
            creada_en="2026-03-01T20:04:00",
            precio_centavos=55000,
            butacas=butacas,
        )
        return butacas_pb2.ReservaResponse(reserva=reserva, butacas=butacas, estado="ACTIVA")


class FakePagosStub(_StubBase):
    """Doble de `pagos.PagosService`."""

    async def ListarMetodosPago(self, request, timeout=None, metadata=None):
        """Los metodos de pago que acepta el recinto."""
        self._grabar("ListarMetodosPago", request, timeout, metadata)
        return pagos_pb2.ListarMetodosPagoResponse(
            metodos=[
                pagos_pb2.MetodoPago(
                    metodo="TARJETA",
                    descripcion="Tarjeta de credito o debito",
                ),
                pagos_pb2.MetodoPago(metodo="PSE", descripcion="Pago en linea con el banco"),
            ]
        )


class FakeEmisionStub(_StubBase):
    """Doble de `emision.EmisionService`."""

    async def Listar(self, request, timeout=None, metadata=None):
        """Las entradas de una saga."""
        self._grabar("Listar", request, timeout, metadata)
        return emision_pb2.EntradasResponse(
            entradas=[
                emision_pb2.Entrada(
                    entrada_id="ent-1",
                    saga_id=request.saga_id,
                    butaca_id="sec-1-A1",
                    fila="A",
                    numero=1,
                    qr="TKT-saga-1-sec-1-A1-abc12345",
                    estado="EMITIDA",
                ),
                emision_pb2.Entrada(
                    entrada_id="ent-2",
                    saga_id=request.saga_id,
                    butaca_id="sec-1-A2",
                    fila="A",
                    numero=2,
                    qr="TKT-saga-1-sec-1-A2-def67890",
                    estado="EMITIDA",
                ),
            ],
            estado="EMITIDA",
            idempotente=False,
        )


class FakeComprasStub(_StubBase):
    """Doble de `compras.ComprasService`."""

    def __init__(self, error: Exception | None = None, idempotente: bool = False) -> None:
        super().__init__(error)
        self.idempotente = idempotente

    async def IniciarCompra(self, request, timeout=None, metadata=None):
        """Inicia la saga de compra."""
        self._grabar("IniciarCompra", request, timeout, metadata)
        cantidad = len(request.butaca_ids)
        compra = compras_pb2.Compra(
            saga_id=request.saga_id,
            estado="CONFIRMADA",
            evento_id=request.evento_id,
            seccion_id=request.seccion_id,
            cantidad=cantidad,
            cliente_id=request.cliente_id,
            error="",
            actualizada_en="2026-03-01T20:05:00",
            monto_total_centavos=55000 * cantidad,
            moneda=request.moneda or "COP",
            pasos=[
                compras_pb2.Paso(
                    nombre="RESERVAR",
                    resultado="OK",
                    detalle="",
                    iniciado_en="2026-03-01T20:04:00",
                    finalizado_en="2026-03-01T20:04:01",
                ),
                compras_pb2.Paso(
                    nombre="COBRAR",
                    resultado="OK",
                    detalle="",
                    iniciado_en="2026-03-01T20:04:01",
                    finalizado_en="2026-03-01T20:04:03",
                ),
                compras_pb2.Paso(
                    nombre="EMITIR",
                    resultado="OK",
                    detalle="",
                    iniciado_en="2026-03-01T20:04:03",
                    finalizado_en="2026-03-01T20:04:05",
                ),
            ],
            compensaciones=[],
        )
        return compras_pb2.CompraResponse(compra=compra, idempotente=self.idempotente)

    async def ConsultarCompra(self, request, timeout=None, metadata=None):
        """Consulta el estado de una saga."""
        self._grabar("ConsultarCompra", request, timeout, metadata)
        compra = compras_pb2.Compra(
            saga_id=request.saga_id,
            estado="CONFIRMADA",
            evento_id="ev-1",
            seccion_id="sec-1",
            cantidad=2,
            cliente_id="cliente-1",
            error="",
            actualizada_en="2026-03-01T20:05:00",
            monto_total_centavos=110000,
            moneda="COP",
            pasos=[
                compras_pb2.Paso(
                    nombre="RESERVAR",
                    resultado="OK",
                    detalle="",
                    iniciado_en="2026-03-01T20:04:00",
                    finalizado_en="2026-03-01T20:04:01",
                )
            ],
            compensaciones=[],
        )
        return compras_pb2.CompraResponse(compra=compra, idempotente=False)
