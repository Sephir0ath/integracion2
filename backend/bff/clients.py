"""Clientes de las dependencias del BFF.

El BFF habla **gRPC** con los cuatro servicios del sistema: `butacas`
(inventario y reservas), `pagos` (metodos de pago), `emision` (entradas) y
`compras` (la saga). Tres decisiones que no son de estilo:

* **El stub se crea dentro del event loop.** Un canal de `grpc.aio`
  pertenece al loop que lo abre; crearlo al construir el cliente lo ataria
  al loop equivocado cuando el cliente se construye fuera del servidor.
* **Toda llamada sale por `common.grpc.unary`**, que aplica el limite de
  tiempo de la configuracion (`GRPC_DEADLINE`): una dependencia lenta
  nunca deja el BFF esperando. Los fallos se traducen a `ServiceError`,
  que es como el BFF responde con el codigo de `common.errors`.
* **La identidad viaja como metadatos** (`x-client-id`, `x-role`): el
  gateway ya valido el token y el BFF lo reenvia sin volver a validarlo.
  `common.grpc.unary` no admite metadatos, asi que la llamada se envuelve
  en un cierre que las anade antes de delegar.

El seam es obligatorio: la suite corre sin los servicios internos
levantados, asi que el stub se puede inyectar en el constructor.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging

import grpc

import butacas_pb2
import butacas_pb2_grpc
import compras_pb2
import compras_pb2_grpc
import emision_pb2
import emision_pb2_grpc
import pagos_pb2
import pagos_pb2_grpc

from common.config import Settings
from common.errors import CODES, ServiceError
from common.grpc import create_channel, unary
from common.logging import log_event

logger = logging.getLogger("bff.clients")

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
    """Traduce el fallo de una dependencia a un error de dominio.

    Los servicios de dominio abortan la llamada con un status gRPC y un
    documento JSON en los detalles (`error.to_dict()`). Ese JSON es el
    fallo exacto: conserva el codigo legible por maquina, para que el BFF
    responda `NOT_FOUND` o `CIRCUIT_OPEN` sin leer texto libre. Un status
    sin codigo cae en el mapeo por status gRPC.
    """
    detalles = error.details() or ""
    codigo_grpc = error.code()
    log_event(
        logger,
        "bff_dependencia_error",
        servicio=servicio,
        grpc_status=codigo_grpc.name,
        detalle=detalles,
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


def _acepta_metadatos(funcion) -> bool:
    """Si la llamada admite el argumento `metadata`."""
    try:
        parametros = inspect.signature(funcion).parameters
    except (TypeError, ValueError):  # pragma: no cover - llamadas dinamicas
        return False
    if "metadata" in parametros:
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parametros.values())


def _con_metadatos(funcion, metadatos):
    """Cierra la llamada del stub para anadirle las metadatos de identidad."""

    async def invocar(peticion, timeout=None):
        if _acepta_metadatos(funcion):
            return await funcion(peticion, timeout=timeout, metadata=tuple(metadatos))
        return await funcion(peticion, timeout=timeout)

    return invocar


class _ClienteGrpc:
    """Comportamiento comun: canal, plazo y traduccion de fallos."""

    # Nombre de la dependencia en los errores y en los logs.
    servicio = "dependencia"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        self._target = ""
        self._deadline = float(settings.grpc_deadline)
        self._channel = channel
        self._stub = stub

    @property
    def deadline(self) -> float:
        """Limite de tiempo de cada llamada a la dependencia."""
        return self._deadline

    @property
    def target(self) -> str:
        """Direccion de la dependencia."""
        return self._target

    def _obtener_stub(self):
        """El stub de la dependencia, abriendo el canal cuando hace falta.

        El canal se abre en el event loop que hace la llamada: un canal de
        `grpc.aio` pertenece al loop que lo creo.
        """
        if self._stub is None:
            if self._channel is None:
                self._channel = create_channel(self._target)
            self._stub = self._crear_stub(self._channel)
        return self._stub

    def _crear_stub(self, channel):
        """El stub de la dependencia (lo implementa cada cliente)."""
        raise NotImplementedError

    async def _llamar(self, metodo: str, peticion, metadatos=None):
        """Una llamada unaria bajo el limite de tiempo de la configuracion.

        Cualquier fallo de la dependencia sale como `ServiceError`: asi el
        BFF puede distinguir "esta dependencia no esta" (503, degradacion)
        de "el dominio se opuso" (404, 409...), y nunca hay un 500 opaco.
        """
        stub = self._obtener_stub()
        invocacion = _con_metadatos(getattr(stub, metodo), metadatos or ())
        try:
            return await unary(
                self._channel,
                invocacion,
                peticion,
                timeout=self._deadline,
            )
        except grpc.aio.AioRpcError as error:
            raise traducir_error(error, self.servicio) from error
        except (asyncio.TimeoutError, TimeoutError) as error:
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"el servicio de {self.servicio} no respondio dentro del limite de {self._deadline}s",
                details={"servicio": self.servicio, "deadline": self._deadline},
            ) from error
        except ServiceError:
            raise
        except Exception as error:  # noqa: BLE001 - una dependencia caida no es un 500
            log_event(
                logger,
                "bff_dependencia_fallo",
                servicio=self.servicio,
                metodo=metodo,
                error=type(error).__name__,
                detalle=str(error),
            )
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"el servicio de {self.servicio} fallo: {error}",
                details={"servicio": self.servicio},
            ) from error

    async def close(self) -> None:
        """Cierra el canal de la dependencia."""
        if self._channel is not None:
            await self._channel.close()


class ClienteButacas(_ClienteGrpc):
    """Cliente del servicio `butacas`: inventario y reservas del recinto."""

    servicio = "butacas"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.butacas_target

    def _crear_stub(self, channel):
        return butacas_pb2_grpc.ButacasServiceStub(channel)

    async def listar_eventos(self, metadatos=None) -> list[dict]:
        """Los eventos del recinto."""
        respuesta = await self._llamar(
            "ListarEventos", butacas_pb2.ListarEventosRequest(), metadatos
        )
        return [_evento_como_datos(evento) for evento in respuesta.eventos]

    async def listar_secciones(self, evento_id: str = "", metadatos=None) -> list[dict]:
        """Las secciones de un evento (o todas, si no se filtra)."""
        respuesta = await self._llamar(
            "ListarSecciones",
            butacas_pb2.ListarSeccionesRequest(evento_id=evento_id or ""),
            metadatos,
        )
        return [_seccion_como_datos(seccion) for seccion in respuesta.secciones]

    async def listar_butacas(self, seccion_id: str, metadatos=None) -> list[dict]:
        """El plano de la sala."""
        respuesta = await self._llamar(
            "ListarButacas",
            butacas_pb2.ListarButacasRequest(seccion_id=seccion_id or ""),
            metadatos,
        )
        return [_butaca_como_datos(butaca) for butaca in respuesta.butacas]

    async def consultar_reserva(self, saga_id: str, metadatos=None) -> dict | None:
        """La reserva de una saga, o `None` si todavia no hay ninguna.

        Una saga sin reserva no es un fallo: es una saga que todavia no
        reservo (o que ya libero sus butacas). El servicio responde con el
        estado `NO_ENCONTRADA` y el BFF lo traduce a `None`.
        """
        respuesta = await self._llamar(
            "ConsultarReserva",
            butacas_pb2.ConsultarReservaRequest(saga_id=saga_id or ""),
            metadatos,
        )
        if respuesta.estado == "NO_ENCONTRADA" or not respuesta.reserva.reserva_id:
            return None
        reserva = respuesta.reserva
        butacas = list(reserva.butacas) or list(respuesta.butacas)
        datos = _reserva_como_datos(reserva)
        datos["butacas"] = [_butaca_como_datos(butaca) for butaca in butacas]
        return datos


class ClientePagos(_ClienteGrpc):
    """Cliente del servicio `pagos`: los metodos de pago del recinto."""

    servicio = "pagos"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.pagos_target

    def _crear_stub(self, channel):
        return pagos_pb2_grpc.PagosServiceStub(channel)

    async def listar_metodos_pago(self, metadatos=None) -> list[dict]:
        """Los metodos de pago que acepta el recinto."""
        respuesta = await self._llamar(
            "ListarMetodosPago", pagos_pb2.ListarMetodosPagoRequest(), metadatos
        )
        return [_metodo_como_datos(metodo) for metodo in respuesta.metodos]


class ClienteEmision(_ClienteGrpc):
    """Cliente del servicio `emision`: las entradas emitidas."""

    servicio = "emision"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.emision_target

    def _crear_stub(self, channel):
        return emision_pb2_grpc.EmisionServiceStub(channel)

    async def listar_entradas(self, saga_id: str, metadatos=None) -> list[dict]:
        """Las entradas de una saga, con su estado."""
        respuesta = await self._llamar(
            "Listar",
            emision_pb2.ListarEntradasRequest(saga_id=saga_id or ""),
            metadatos,
        )
        return [_entrada_como_datos(entrada) for entrada in respuesta.entradas]


class ClienteCompras(_ClienteGrpc):
    """Cliente del orquestador de la saga (`compras`)."""

    servicio = "compras"

    def __init__(self, settings: Settings, stub=None, channel=None) -> None:
        super().__init__(settings, stub=stub, channel=channel)
        self._target = settings.compras_target

    def _crear_stub(self, channel):
        return compras_pb2_grpc.ComprasServiceStub(channel)

    async def iniciar_compra(
        self,
        saga_id: str,
        evento_id: str,
        seccion_id: str,
        butaca_ids: list[str],
        cliente_id: str,
        moneda: str = "COP",
        metadatos=None,
    ) -> dict:
        """Inicia la saga de compra (la operacion central del sistema)."""
        respuesta = await self._llamar(
            "IniciarCompra",
            compras_pb2.IniciarCompraRequest(
                saga_id=saga_id,
                evento_id=evento_id,
                seccion_id=seccion_id,
                butaca_ids=list(butaca_ids or []),
                cliente_id=cliente_id,
                moneda=moneda or "COP",
            ),
            metadatos,
        )
        return _compra_como_datos(respuesta.compra)

    async def consultar_compra(self, saga_id: str, metadatos=None) -> dict:
        """El estado de una saga de compra."""
        respuesta = await self._llamar(
            "ConsultarCompra",
            compras_pb2.ConsultarCompraRequest(saga_id=saga_id or ""),
            metadatos,
        )
        return _compra_como_datos(respuesta.compra)


# ------------------------------------------------------------- auxiliares --
def _evento_como_datos(evento) -> dict:
    """Evento -> dict, la forma del contrato del borde."""
    return {
        "evento_id": evento.evento_id,
        "nombre": evento.nombre,
        "lugar": evento.lugar,
        "fecha": evento.fecha,
    }


def _seccion_como_datos(seccion) -> dict:
    """Seccion -> dict, la forma del contrato del borde."""
    return {
        "seccion_id": seccion.seccion_id,
        "evento_id": seccion.evento_id,
        "nombre": seccion.nombre,
        "capacidad_total": int(seccion.capacidad_total),
        "cantidad_disponible": int(seccion.cantidad_disponible),
        "precio_centavos": int(seccion.precio_centavos),
    }


def _butaca_como_datos(butaca) -> dict:
    """Butaca -> dict, la forma del contrato del borde."""
    return {
        "butaca_id": butaca.butaca_id,
        "seccion_id": butaca.seccion_id,
        "fila": butaca.fila,
        "numero": int(butaca.numero),
        "estado": butaca.estado,
        "saga_id": butaca.saga_id,
    }


def _reserva_como_datos(reserva) -> dict:
    """Reserva -> dict, la forma del contrato del borde."""
    return {
        "reserva_id": reserva.reserva_id,
        "saga_id": reserva.saga_id,
        "seccion_id": reserva.seccion_id,
        "estado": reserva.estado,
        "precio_centavos": int(reserva.precio_centavos),
        "creada_en": reserva.creada_en,
        "butacas": [_butaca_como_datos(butaca) for butaca in reserva.butacas],
    }


def _metodo_como_datos(metodo) -> dict:
    """Metodo de pago -> dict, la forma del contrato del borde."""
    return {
        "metodo": metodo.metodo,
        "descripcion": metodo.descripcion,
    }


def _compra_como_datos(compra) -> dict:
    """Compra -> dict, la forma del contrato del borde."""
    return {
        "saga_id": compra.saga_id,
        "estado": compra.estado,
        "pasos": [
            {
                "nombre": paso.nombre,
                "resultado": paso.resultado,
                "detalle": paso.detalle,
                "iniciado_en": paso.iniciado_en,
                "finalizado_en": paso.finalizado_en,
            }
            for paso in compra.pasos
        ],
        "compensaciones": [
            {
                "nombre": compensacion.nombre,
                "resultado": compensacion.resultado,
                "detalle": compensacion.detalle,
                "finalizado_en": compensacion.finalizado_en,
            }
            for compensacion in compra.compensaciones
        ],
        "evento_id": compra.evento_id,
        "seccion_id": compra.seccion_id,
        "cantidad": int(compra.cantidad),
        "cliente_id": compra.cliente_id,
        "error": compra.error,
        "actualizada_en": compra.actualizada_en,
        "monto_total_centavos": int(compra.monto_total_centavos),
        "moneda": compra.moneda,
    }


def _entrada_como_datos(entrada) -> dict:
    """Entrada -> dict, la forma del contrato del borde."""
    return {
        "entrada_id": entrada.entrada_id,
        "saga_id": entrada.saga_id,
        "butaca_id": entrada.butaca_id,
        "fila": entrada.fila,
        "numero": int(entrada.numero),
        "qr": entrada.qr,
        "estado": entrada.estado,
    }
