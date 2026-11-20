"""Dobles de las dependencias externas del gateway.

Los dobles tienen los mismos metodos que los servicios reales:

* los stubs gRPC responden como los servicios de dominio y **registran el
  limite de tiempo y las metadatos que reciben**, para que la prueba pueda
  comprobar que toda llamada lleva su deadline y que la identidad del
  cliente viaja al servicio interno;
* el doble de Redis ejecuta la misma semantica que el script Lua del
  limitador (la funcion pura `consumir_tokens`, que es la referencia
  documentada del script) y puede fallar como falla un Redis caido;
* el doble del BFF responde con el estado y el cuerpo que se le pidan.
"""

from __future__ import annotations

import json
import time

import grpc

import butacas_pb2
import compras_pb2
import emision_pb2

# ---------------------------------------------------------------- eventos --


def evento(evento_id: str, nombre: str, lugar: str, fecha: str) -> butacas_pb2.Evento:
    """Evento de prueba."""
    return butacas_pb2.Evento(evento_id=evento_id, nombre=nombre, lugar=lugar, fecha=fecha)


def seccion(
    seccion_id: str,
    evento_id: str,
    nombre: str,
    capacidad_total: int,
    cantidad_disponible: int,
    precio_centavos: int,
) -> butacas_pb2.Seccion:
    """Seccion de prueba."""
    return butacas_pb2.Seccion(
        seccion_id=seccion_id,
        evento_id=evento_id,
        nombre=nombre,
        capacidad_total=capacidad_total,
        cantidad_disponible=cantidad_disponible,
        precio_centavos=precio_centavos,
    )


def butaca(
    butaca_id: str,
    seccion_id: str,
    fila: str,
    numero: int,
    estado: str,
    saga_id: str = "",
) -> butacas_pb2.Butaca:
    """Butaca de prueba."""
    return butacas_pb2.Butaca(
        butaca_id=butaca_id,
        seccion_id=seccion_id,
        fila=fila,
        numero=numero,
        estado=estado,
        saga_id=saga_id,
    )


def error_grpc(
    codigo: grpc.StatusCode = grpc.StatusCode.UNAVAILABLE,
    detalle: str = "el servicio no esta disponible",
) -> grpc.aio.AioRpcError:
    """Error de un servicio interno que no puede responder."""
    return grpc.aio.AioRpcError(codigo, None, None, details=detalle)


def error_grpc_con_codigo(codigo_dominio: str, mensaje: str) -> grpc.aio.AioRpcError:
    """Error de un servicio interno con el codigo de `common.errors`."""
    return error_grpc(
        grpc.StatusCode.UNAVAILABLE,
        json.dumps({"code": codigo_dominio, "message": mensaje, "details": {}}),
    )


# ------------------------------------------------------------ stub gRPC --


class _StubBase:
    """Comportamiento comun de los stubs: registro de llamadas y error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    def _grabar(self, metodo: str, request, timeout, metadata) -> None:
        self.calls.append(
            {
                "metodo": metodo,
                "timeout": timeout,
                "metadata": {str(clave).lower(): valor for clave, valor in dict(metadata or {}).items()},
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
                evento("ev-1", "Partido decisivo", "El Campin", "2026-03-01T20:00:00"),
                evento("ev-2", "Concierto sinfonico", "Coliseo", "2026-04-01T20:00:00"),
            ]
        )

    async def ListarSecciones(self, request, timeout=None, metadata=None):
        """Las secciones de un evento."""
        self._grabar("ListarSecciones", request, timeout, metadata)
        return butacas_pb2.ListarSeccionesResponse(
            secciones=[
                seccion("sec-1", "ev-1", "Tribuna norte", 32, 30, 55000),
                seccion("sec-2", "ev-1", "Platea", 48, 48, 120000),
            ]
        )

    async def ListarButacas(self, request, timeout=None, metadata=None):
        """El plano de la sala."""
        self._grabar("ListarButacas", request, timeout, metadata)
        return butacas_pb2.ListarButacasResponse(
            butacas=[
                butaca("sec-1-A1", "sec-1", "A", 1, "LIBRE"),
                butaca("sec-1-A2", "sec-1", "A", 2, "RESERVADA", "saga-1"),
                butaca("sec-1-A3", "sec-1", "A", 3, "VENDIDA", "saga-0"),
            ]
        )

    async def CrearEvento(self, request, timeout=None, metadata=None):
        """Crea un evento."""
        self._grabar("CrearEvento", request, timeout, metadata)
        return evento("ev-9", request.nombre, request.lugar, request.fecha)

    async def ActualizarEvento(self, request, timeout=None, metadata=None):
        """Actualiza un evento."""
        self._grabar("ActualizarEvento", request, timeout, metadata)
        return evento(request.evento_id, request.nombre, request.lugar, request.fecha)

    async def EliminarEvento(self, request, timeout=None, metadata=None):
        """Elimina un evento."""
        self._grabar("EliminarEvento", request, timeout, metadata)
        return butacas_pb2.EliminarResponse(eliminado=True)

    async def CrearSeccion(self, request, timeout=None, metadata=None):
        """Crea una seccion."""
        self._grabar("CrearSeccion", request, timeout, metadata)
        return seccion("sec-9", request.evento_id, request.nombre, request.capacidad_total, request.capacidad_total, request.precio_centavos)

    async def ActualizarSeccion(self, request, timeout=None, metadata=None):
        """Actualiza una seccion."""
        self._grabar("ActualizarSeccion", request, timeout, metadata)
        return seccion(
            request.seccion_id,
            "ev-1",
            request.nombre,
            request.capacidad_total,
            request.capacidad_total,
            request.precio_centavos,
        )

    async def EliminarSeccion(self, request, timeout=None, metadata=None):
        """Elimina una seccion."""
        self._grabar("EliminarSeccion", request, timeout, metadata)
        return butacas_pb2.EliminarResponse(eliminado=True)


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
                compras_pb2.Paso(nombre="RESERVAR", resultado="OK", detalle="", iniciado_en="a", finalizado_en="b"),
                compras_pb2.Paso(nombre="COBRAR", resultado="OK", detalle="", iniciado_en="a", finalizado_en="b"),
                compras_pb2.Paso(nombre="EMITIR", resultado="OK", detalle="", iniciado_en="a", finalizado_en="b"),
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
            pasos=[compras_pb2.Paso(nombre="RESERVAR", resultado="OK")],
            compensaciones=[],
        )
        return compras_pb2.CompraResponse(compra=compra, idempotente=False)


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


# --------------------------------------------------------------- redis --


class FakeRedis:
    """Doble de Redis con la semantica del script Lua del limitador.

    `eval` ejecuta la misma cuenta que el script (la funcion pura
    `consumir_tokens`, que es la referencia documentada del script) sobre
    los cubos que guarda en memoria. Con `error` el doble falla como
    falla un Redis caido, que es lo que ejercita el respaldo en memoria
    del limitador.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.cubos: dict[str, tuple[float, float]] = {}
        self.evaluaciones = 0

    async def eval(self, script, numkeys, *args):
        """Ejecuta el script del limitador."""
        self.evaluaciones += 1
        if self.error is not None:
            raise self.error

        from ..rate_limit import consumir_tokens

        clave = str(args[0])
        capacidad = float(args[1])
        recarga = float(args[2])
        costo = float(args[3])
        # El script usa el reloj del servidor de Redis, en segundos.
        ahora = time.time()
        tokens, marca = self.cubos.get(clave, (capacidad, ahora))
        permitido, tokens, marca = consumir_tokens(tokens, marca, ahora, capacidad, recarga, costo)
        self.cubos[clave] = (tokens, marca)
        return [1 if permitido else 0, str(tokens)]
