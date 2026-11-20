"""Clientes de las dependencias del gateway.

El gateway habla **gRPC** con los servicios de dominio (butacas, compras,
emision) y **HTTP** con el BFF. Dos decisiones que no son de estilo:

* **El stub se crea dentro del event loop.** Un canal de `grpc.aio`
  pertenece al loop que lo abre; crearlo al construir el cliente lo
  ataria al loop equivocado cuando el cliente se construye fuera del
  servidor.
* **La identidad viaja como metadatos.** El gateway ya valido el token,
  asi que el servicio interno recibe `x-client-id` y `x-role` y no vuelve
  a validar nada. `common.grpc.unary` no admite metadatos, asi que la
  llamada se envuelve en un cierre que las anade antes de delegar.

Todas las llamadas gRPC salen por `common.grpc.unary`, que aplica el
limite de tiempo de la configuracion (`GRPC_DEADLINE`): ninguna dependencia
lenta deja el gateway esperando. Los fallos se traducen a `ServiceError`,
que es como el gateway responde con el codigo de `common.errors`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass

import grpc
import httpx

import butacas_pb2
import butacas_pb2_grpc
import compras_pb2
import compras_pb2_grpc
import emision_pb2
import emision_pb2_grpc

from common.config import Settings
from common.errors import CODES, ServiceError
from common.grpc import create_channel, unary
from common.http_client import http_client
from common.logging import log_event

logger = logging.getLogger("gateway.clients")

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
    fallo exacto: conserva el codigo legible por maquina, para que el
    gateway responda `ALREADY_EXISTS` o `CIRCUIT_OPEN` sin leer texto
    libre. Un status sin codigo cae en el mapeo por status gRPC.
    """
    detalles = error.details() or ""
    codigo_grpc = error.code()
    log_event(
        logger,
        "gateway_dependencia_error",
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
        """Una llamada unaria bajo el limite de tiempo de la configuracion."""
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

    async def close(self) -> None:
        """Cierra el canal de la dependencia."""
        if self._channel is not None:
            await self._channel.close()


class ClienteButacas(_ClienteGrpc):
    """Cliente del servicio `butacas`: inventario del recinto."""

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
        """Las secciones de un evento."""
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

    async def crear_evento(self, nombre: str, lugar: str, fecha: str, metadatos=None) -> dict:
        """Crea un evento."""
        respuesta = await self._llamar(
            "CrearEvento",
            butacas_pb2.CrearEventoRequest(nombre=nombre, lugar=lugar, fecha=fecha),
            metadatos,
        )
        return _evento_como_datos(respuesta)

    async def actualizar_evento(
        self, evento_id: str, nombre: str, lugar: str, fecha: str, metadatos=None
    ) -> dict:
        """Actualiza un evento."""
        respuesta = await self._llamar(
            "ActualizarEvento",
            butacas_pb2.ActualizarEventoRequest(
                evento_id=evento_id, nombre=nombre, lugar=lugar, fecha=fecha
            ),
            metadatos,
        )
        return _evento_como_datos(respuesta)

    async def eliminar_evento(self, evento_id: str, metadatos=None) -> dict:
        """Elimina un evento que no se vendio."""
        respuesta = await self._llamar(
            "EliminarEvento",
            butacas_pb2.EliminarEventoRequest(evento_id=evento_id),
            metadatos,
        )
        return {"eliminado": bool(respuesta.eliminado)}

    async def crear_seccion(
        self,
        evento_id: str,
        nombre: str,
        capacidad_total: int,
        precio_centavos: int,
        metadatos=None,
    ) -> dict:
        """Crea una seccion y su plano."""
        respuesta = await self._llamar(
            "CrearSeccion",
            butacas_pb2.CrearSeccionRequest(
                evento_id=evento_id,
                nombre=nombre,
                capacidad_total=int(capacidad_total),
                precio_centavos=int(precio_centavos),
            ),
            metadatos,
        )
        return _seccion_como_datos(respuesta)

    async def actualizar_seccion(
        self,
        seccion_id: str,
        nombre: str,
        capacidad_total: int | None,
        precio_centavos: int | None,
        metadatos=None,
    ) -> dict:
        """Actualiza una seccion y ajusta su plano.

        Un valor en cero significa "no cambiar": el servicio de butacas
        interpreta el cero como "dejar como esta".
        """
        respuesta = await self._llamar(
            "ActualizarSeccion",
            butacas_pb2.ActualizarSeccionRequest(
                seccion_id=seccion_id,
                nombre=nombre,
                capacidad_total=int(capacidad_total or 0),
                precio_centavos=int(precio_centavos or 0),
            ),
            metadatos,
        )
        return _seccion_como_datos(respuesta)

    async def eliminar_seccion(self, seccion_id: str, metadatos=None) -> dict:
        """Elimina una seccion que no se vendio."""
        respuesta = await self._llamar(
            "EliminarSeccion",
            butacas_pb2.EliminarSeccionRequest(seccion_id=seccion_id),
            metadatos,
        )
        return {"eliminado": bool(respuesta.eliminado)}


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
        return {
            "compra": _compra_como_datos(respuesta.compra),
            "idempotente": bool(respuesta.idempotente),
        }

    async def consultar_compra(self, saga_id: str, metadatos=None) -> dict:
        """El estado de una saga de compra."""
        respuesta = await self._llamar(
            "ConsultarCompra",
            compras_pb2.ConsultarCompraRequest(saga_id=saga_id),
            metadatos,
        )
        return {"compra": _compra_como_datos(respuesta.compra)}


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
            emision_pb2.ListarEntradasRequest(saga_id=saga_id),
            metadatos,
        )
        return [_entrada_como_datos(entrada) for entrada in respuesta.entradas]


@dataclass(frozen=True)
class RespuestaBff:
    """La respuesta del BFF, tal cual la devolvio."""

    estado: int
    cuerpo: bytes
    tipo_contenido: str | None = None


class ClienteBff:
    """Cliente HTTP del BFF (la agregacion por cliente)."""

    def __init__(self, settings: Settings, cliente=None) -> None:
        self._url_base = settings.bff_url.rstrip("/")
        self._timeout = float(settings.grpc_deadline)
        self._cliente = cliente
        self._propio = cliente is None

    @property
    def url_base(self) -> str:
        """Direccion del BFF."""
        return self._url_base

    def _obtener_cliente(self) -> httpx.AsyncClient:
        """El cliente HTTP, creado la primera vez que se usa."""
        if self._cliente is None:
            self._cliente = http_client(timeout=self._timeout)
        return self._cliente

    async def enviar(
        self,
        metodo: str,
        ruta: str,
        params=None,
        cuerpo: bytes | None = None,
        cabeceras: dict | None = None,
    ) -> RespuestaBff:
        """Le pasa la peticion al BFF y devuelve su respuesta intacta."""
        cliente = self._obtener_cliente()
        url = f"{self._url_base}/{ruta.lstrip('/')}"
        try:
            respuesta = await cliente.request(
                metodo,
                url,
                params=params,
                content=cuerpo,
                headers=cabeceras,
            )
        except httpx.HTTPError as error:
            raise ServiceError(
                CODES.DEPENDENCY_UNAVAILABLE,
                f"el BFF no esta disponible: {error}",
                details={"bff": self._url_base},
            ) from error
        return RespuestaBff(
            estado=respuesta.status_code,
            cuerpo=respuesta.content,
            tipo_contenido=respuesta.headers.get("content-type"),
        )

    async def close(self) -> None:
        """Cierra el cliente HTTP, si el gateway lo creo."""
        if self._propio and self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None


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
                "finalizado_en": compensacion.finalizada_en,
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
