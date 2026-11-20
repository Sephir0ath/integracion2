"""Aplicacion HTTP del gateway.

El gateway es el **unico punto de entrada** del sistema: autentica, limita
y enruta. Los servicios de dominio no se exponen y confian en que el
gateway ya valido el token.

Tabla de rutas (el contrato del borde):

| Metodo + ruta                                  | Auth      | Que hace                                  |
|-----------------------------------------------|-----------|-------------------------------------------|
| `POST /api/v1/auth/token`                      | publico   | emite el token del cliente                |
| `GET /healthz`                                 | publico   | liveness del gateway                      |
| `GET /metrics`                                 | publico   | texto Prometheus del registro compartido  |
| `GET /api/v1/butacas/eventos`                  | bearer    | `butacas.ListarEventos`                   |
| `GET /api/v1/butacas/secciones`                | bearer    | `butacas.ListarSecciones`                 |
| `GET /api/v1/butacas/secciones/{id}/butacas`  | bearer    | `butacas.ListarButacas`                   |
| `POST /api/v1/butacas/eventos`                 | bearer+admin | `butacas.CrearEvento`                 |
| `PUT /api/v1/butacas/eventos/{id}`             | bearer+admin | `butacas.ActualizarEvento`            |
| `DELETE /api/v1/butacas/eventos/{id}`          | bearer+admin | `butacas.EliminarEvento`               |
| `POST /api/v1/butacas/secciones`               | bearer+admin | `butacas.CrearSeccion`                |
| `PUT /api/v1/butacas/secciones/{id}`           | bearer+admin | `butacas.ActualizarSeccion`            |
| `DELETE /api/v1/butacas/secciones/{id}`        | bearer+admin | `butacas.EliminarSeccion`             |
| `POST /api/v1/compras`                         | bearer    | `compras.IniciarCompra` (201)             |
| `GET /api/v1/compras/{saga_id}`                | bearer    | `compras.ConsultarCompra`                |
| `GET /api/v1/entradas`                         | bearer    | `emision.Listar`                          |
| `GET|POST /bff/web/**`                         | bearer    | la ruta equivalente del BFF               |

El token se valida **una sola vez**, en la entrada, y la identidad del
cliente viaja al servicio interno en `X-Client-Id` y `X-Role`: los
servicios de dominio no vuelven a validar el token.

Todas las dependencias externas entran por el constructor (seams
inyectables): el cliente gRPC, el cliente de Redis y el cliente HTTP del
BFF. Asi la suite corre sin Redis y sin los servicios internos levantados.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from common.errors import ServiceError
from common.logging import log_event, set_request_id
from common.metrics import REGISTRY

from .auth import Principal, extraer_token, verificar_token
from .auth import emitir_token as firmar_token
from .clients import ClienteBff, ClienteButacas, ClienteCompras, ClienteEmision
from .config import ConfiguracionGateway
from .errores import (
    CODIGO_INVALIDO,
    CODIGO_LIMITE,
    CODIGO_NO_AUTENTICADO,
    CODIGO_SIN_PERMISO,
    ErrorGateway,
)
from .metricas import duracion, fallos_autenticacion, rechazos_limite, solicitudes
from .rate_limit import LimitadorTazon
from .usuarios import AlmacenUsuarios

logger = logging.getLogger("gateway.app")

# Cabeceras con las que viaja la identidad del cliente.
CABECERA_CLIENTE = "X-Client-Id"
CABECERA_ROL = "X-Role"


# ---------------------------------------------------------------- cuerpos --
class CuerpoToken(BaseModel):
    """Cuerpo de `POST /api/v1/auth/token`."""

    email: str
    password: str


class CuerpoEvento(BaseModel):
    """Cuerpo del CRUD de eventos."""

    nombre: str
    lugar: str = ""
    fecha: str = ""


class CuerpoSeccion(BaseModel):
    """Cuerpo del CRUD de secciones."""

    evento_id: str = ""
    nombre: str = ""
    capacidad_total: int = 0
    precio_centavos: int = 0


class CuerpoCompra(BaseModel):
    """Cuerpo de `POST /api/v1/compras`.

    El `cliente_id` no viene del cuerpo: lo pone el gateway desde el
    token, porque el cliente no puede comprar en nombre de otro.
    """

    saga_id: str = ""
    evento_id: str = ""
    seccion_id: str = ""
    butaca_ids: list[str] = []
    moneda: str = "COP"


# -------------------------------------------------------------- aplicacion --
def crear_app(
    config: ConfiguracionGateway | None = None,
    *,
    almacen: AlmacenUsuarios | None = None,
    butacas: ClienteButacas | None = None,
    compras: ClienteCompras | None = None,
    emision: ClienteEmision | None = None,
    limitador: LimitadorTazon | None = None,
    cliente_bff: ClienteBff | None = None,
) -> FastAPI:
    """Construye la aplicacion del gateway.

    Las tres dependencias externas se pueden inyectar: sin inyeccion se
    usan las reales (gRPC contra los servicios internos, Redis para el
    limite de peticiones y HTTP contra el BFF).
    """
    config = config or ConfiguracionGateway.from_env()
    settings = config.settings

    almacen = almacen or AlmacenUsuarios(config.usuarios)
    butacas = butacas or ClienteButacas(settings)
    compras = compras or ClienteCompras(settings)
    emision = emision or ClienteEmision(settings)
    cliente_bff = cliente_bff or ClienteBff(settings)

    redis = None
    if limitador is None:
        redis = _cliente_redis(settings)
        limitador = LimitadorTazon(
            redis=redis,
            capacidad=settings.rate_limit_burst,
            recarga_por_minuto=settings.rate_limit_per_minute,
        )

    @asynccontextmanager
    async def _vida(_app):
        """Abre y cierra lo que el proceso abrio."""
        log_event(
            logger,
            "gateway_iniciando",
            puerto=config.puerto,
            usuarios=len(almacen),
            burst=settings.rate_limit_burst,
            limite_por_minuto=settings.rate_limit_per_minute,
            butacas=settings.butacas_target,
            compras=settings.compras_target,
            emision=settings.emision_target,
            bff=settings.bff_url,
        )
        try:
            yield
        finally:
            await _cerrar_redis(redis)
            await cliente_bff.close()
            for cliente in (butacas, compras, emision):
                await cliente.close()

    app = FastAPI(
        title="Gateway de la ticketera",
        docs_url=None,
        redoc_url=None,
        lifespan=_vida,
    )

    # ------------------------------------------------------- observabilidad --
    @app.middleware("http")
    async def _observar(request: Request, call_next):
        """Cuenta cada peticion y le pone un id de correlacion."""
        identificador = request.headers.get("x-request-id") or uuid.uuid4().hex
        set_request_id(identificador)
        request.state.request_id = identificador
        inicio = time.perf_counter()
        try:
            respuesta = await call_next(request)
        except Exception:
            _contar(request, 500, time.perf_counter() - inicio)
            raise
        _contar(request, respuesta.status_code, time.perf_counter() - inicio)
        respuesta.headers["X-Request-Id"] = identificador
        return respuesta

    # ------------------------------------------------------------ errores --
    @app.exception_handler(ErrorGateway)
    async def _error_del_borde(request: Request, error: ErrorGateway):
        """Los errores del gateway, con su codigo y sus cabeceras."""
        return JSONResponse(
            {"code": error.codigo, "message": error.mensaje},
            status_code=error.estado,
            headers=error.cabeceras,
        )

    @app.exception_handler(ServiceError)
    async def _error_de_dominio(request: Request, error: ServiceError):
        """Los errores que vienen traducidos desde los servicios internos."""
        return JSONResponse(
            {"code": error.code, "message": error.message},
            status_code=error.http_status,
        )

    @app.exception_handler(RequestValidationError)
    async def _peticion_invalida(request: Request, error: RequestValidationError):
        """Un cuerpo que no cumple el contrato es un 400, no un 422."""
        return JSONResponse(
            {
                "code": CODIGO_INVALIDO,
                "message": "la peticion no es valida",
                "details": {"errores": _errores_de_validacion(error)},
            },
            status_code=400,
        )

    # -------------------------------------------------------- dependencias --
    async def _principal(authorization: str | None = Header(default=None)) -> Principal:
        """Valida el token de la peticion."""
        token = extraer_token(authorization)
        if token is None:
            fallos_autenticacion.inc()
            raise ErrorGateway(
                401,
                CODIGO_NO_AUTENTICADO,
                "se necesita un token valido en la cabecera Authorization",
            )
        try:
            return verificar_token(token, settings)
        except ErrorGateway:
            fallos_autenticacion.inc()
            raise

    async def _limitado(principal: Principal = Depends(_principal)) -> Principal:
        """Aplica el limite de peticiones del cliente."""
        decision = await limitador.permitir(principal.cliente_id)
        if not decision.permitido:
            rechazos_limite.inc()
            log_event(
                logger,
                "gateway_limite_excedido",
                cliente_id=principal.cliente_id,
                almacen=decision.almacen,
                reintento_tras=decision.reintento_tras,
            )
            raise ErrorGateway(
                429,
                CODIGO_LIMITE,
                "se supero el limite de peticiones del cliente",
                {"Retry-After": str(decision.reintento_tras)},
            )
        return principal

    async def _admin(principal: Principal = Depends(_limitado)) -> Principal:
        """Exige el rol `admin` para las rutas de administracion."""
        if not principal.es_admin:
            raise ErrorGateway(
                403,
                CODIGO_SIN_PERMISO,
                "la operacion es de administracion del recinto",
            )
        return principal

    # -------------------------------------------------------------- publico --
    @app.post("/api/v1/auth/token")
    async def token_de_acceso(cuerpo: CuerpoToken):
        """Emite el token del cliente con sus credenciales.

        El limite por cliente no aplica aqui: sin token todavia no hay
        `cliente_id` con el que limitar. Es el unico endpoint publico
        besides `healthz` y `metrics`, y queda expuesto a un ataque de
        fuerza bruta: si el equipo lo decide, el limite puede pasar a
        ser por direccion IP.
        """
        usuario = almacen.autenticar(cuerpo.email, cuerpo.password)
        if usuario is None:
            fallos_autenticacion.inc()
            raise ErrorGateway(
                401,
                CODIGO_NO_AUTENTICADO,
                "correo o contrasena incorrectos",
            )
        token, expires_in = firmar_token(usuario, settings)
        log_event(
            logger,
            "gateway_token_emitido",
            cliente_id=usuario.cliente_id,
            rol=usuario.role,
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
            "cliente_id": usuario.cliente_id,
            "role": usuario.role,
        }

    @app.get("/healthz")
    async def healthz():
        """Liveness del gateway."""
        return {
            "status": "ok",
            "service": settings.service_name,
            "usuarios": len(almacen),
            "burst": settings.rate_limit_burst,
            "limite_por_minuto": settings.rate_limit_per_minute,
        }

    @app.get("/metrics")
    async def metrics():
        """El texto Prometheus del registro compartido."""
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # -------------------------------------------------------------- butacas --
    @app.get("/api/v1/butacas/eventos")
    async def listar_eventos(principal: Principal = Depends(_limitado)):
        """Los eventos del recinto."""
        return {"eventos": await butacas.listar_eventos(principal.metadatos())}

    @app.get("/api/v1/butacas/secciones")
    async def listar_secciones(
        evento_id: str = Query(default=""),
        principal: Principal = Depends(_limitado),
    ):
        """Las secciones de un evento."""
        return {"secciones": await butacas.listar_secciones(evento_id, principal.metadatos())}

    @app.get("/api/v1/butacas/secciones/{seccion_id}/butacas")
    async def listar_butacas(seccion_id: str, principal: Principal = Depends(_limitado)):
        """El plano de la sala."""
        return {"butacas": await butacas.listar_butacas(seccion_id, principal.metadatos())}

    @app.post("/api/v1/butacas/eventos")
    async def crear_evento(cuerpo: CuerpoEvento, principal: Principal = Depends(_admin)):
        """Crea un evento."""
        evento = await butacas.crear_evento(
            cuerpo.nombre, cuerpo.lugar, cuerpo.fecha, principal.metadatos()
        )
        return {"evento": evento}

    @app.put("/api/v1/butacas/eventos/{evento_id}")
    async def actualizar_evento(
        evento_id: str, cuerpo: CuerpoEvento, principal: Principal = Depends(_admin)
    ):
        """Actualiza un evento."""
        evento = await butacas.actualizar_evento(
            evento_id, cuerpo.nombre, cuerpo.lugar, cuerpo.fecha, principal.metadatos()
        )
        return {"evento": evento}

    @app.delete("/api/v1/butacas/eventos/{evento_id}")
    async def eliminar_evento(evento_id: str, principal: Principal = Depends(_admin)):
        """Elimina un evento que no se vendio."""
        return await butacas.eliminar_evento(evento_id, principal.metadatos())

    @app.post("/api/v1/butacas/secciones")
    async def crear_seccion(cuerpo: CuerpoSeccion, principal: Principal = Depends(_admin)):
        """Crea una seccion y su plano."""
        seccion = await butacas.crear_seccion(
            cuerpo.evento_id,
            cuerpo.nombre,
            cuerpo.capacidad_total,
            cuerpo.precio_centavos,
            principal.metadatos(),
        )
        return {"seccion": seccion}

    @app.put("/api/v1/butacas/secciones/{seccion_id}")
    async def actualizar_seccion(
        seccion_id: str, cuerpo: CuerpoSeccion, principal: Principal = Depends(_admin)
    ):
        """Actualiza una seccion y ajusta su plano."""
        seccion = await butacas.actualizar_seccion(
            seccion_id,
            cuerpo.nombre,
            cuerpo.capacidad_total,
            cuerpo.precio_centavos,
            principal.metadatos(),
        )
        return {"seccion": seccion}

    @app.delete("/api/v1/butacas/secciones/{seccion_id}")
    async def eliminar_seccion(seccion_id: str, principal: Principal = Depends(_admin)):
        """Elimina una seccion que no se vendio."""
        return await butacas.eliminar_seccion(seccion_id, principal.metadatos())

    # -------------------------------------------------------------- compras --
    @app.post("/api/v1/compras", status_code=201)
    async def iniciar_compra(cuerpo: CuerpoCompra, principal: Principal = Depends(_limitado)):
        """Inicia la compra (la saga orquestada por `compras`)."""
        saga_id = cuerpo.saga_id or uuid.uuid4().hex
        return await compras.iniciar_compra(
            saga_id,
            cuerpo.evento_id,
            cuerpo.seccion_id,
            cuerpo.butaca_ids,
            principal.cliente_id,
            cuerpo.moneda,
            principal.metadatos(),
        )

    @app.get("/api/v1/compras/{saga_id}")
    async def consultar_compra(saga_id: str, principal: Principal = Depends(_limitado)):
        """El estado de una compra."""
        return await compras.consultar_compra(saga_id, principal.metadatos())

    # ------------------------------------------------------------- entradas --
    @app.get("/api/v1/entradas")
    async def listar_entradas(
        saga_id: str = Query(...),
        principal: Principal = Depends(_limitado),
    ):
        """Las entradas de una saga."""
        return {"entradas": await emision.listar_entradas(saga_id, principal.metadatos())}

    # ----------------------------------------------------------------- bff --
    @app.api_route("/bff/web/{ruta:path}", methods=["GET", "POST"])
    async def proxy_del_bff(ruta: str, request: Request, principal: Principal = Depends(_limitado)):
        """Le pasa la peticion al BFF y devuelve su respuesta intacta."""
        cuerpo = await request.body() if request.method == "POST" else None
        parametros = list(request.query_params.multi_items())
        cabeceras = {
            CABECERA_CLIENTE: principal.cliente_id,
            CABECERA_ROL: principal.role,
        }
        # El BFF es otro servicio interno: recibe la identidad del cliente
        # y tambien el token que el gateway ya valido, por si el BFF
        # quiere comprobarlo por su cuenta.
        autorizacion = request.headers.get("authorization")
        if autorizacion:
            cabeceras["Authorization"] = autorizacion

        respuesta = await cliente_bff.enviar(
            request.method,
            f"/bff/web/{ruta}",
            params=parametros or None,
            cuerpo=cuerpo,
            cabeceras=cabeceras,
        )
        cabeceras_respuesta = {}
        if respuesta.tipo_contenido:
            cabeceras_respuesta["content-type"] = respuesta.tipo_contenido
        return Response(
            content=respuesta.cuerpo,
            status_code=respuesta.estado,
            headers=cabeceras_respuesta,
        )

    return app


# ------------------------------------------------------------- auxiliares --
def _contar(request: Request, estado: int, segundos: float) -> None:
    """Cuenta la peticion en las metricas del gateway."""
    ruta = _plantilla_de_la_ruta(request)
    solicitudes.labels(ruta, request.method, str(estado)).inc()
    duracion.labels(ruta).observe(segundos)


def _plantilla_de_la_ruta(request: Request) -> str:
    """La plantilla de la ruta (`/api/v1/compras/{saga_id}`).

    Se usa la plantilla y no la ruta concreta para que las series no
    crezcan con cada identificacion de saga o de butaca.
    """
    ruta = getattr(request.scope.get("route"), "path", None)
    return str(ruta) if ruta else "sin_coincidencia"


def _errores_de_validacion(error: RequestValidationError) -> list[dict]:
    """Los errores de validacion del cuerpo, en forma legible."""
    detalles = []
    for fallo in error.errors():
        detalles.append(
            {
                "campo": ".".join(str(parte) for parte in fallo.get("loc", ())),
                "mensaje": str(fallo.get("msg", "")),
            }
        )
    return detalles


def _cliente_redis(settings):
    """El cliente de Redis del limitador, si el sistema lo tiene configurado."""
    if not settings.redis_url:
        return None
    import redis.asyncio as redis_asyncio

    return redis_asyncio.from_url(settings.redis_url, decode_responses=True)


async def _cerrar_redis(redis) -> None:
    """Cierra el cliente de Redis, si lo hubo."""
    if redis is None:
        return
    cerrar = getattr(redis, "aclose", None) or getattr(redis, "close", None)
    if cerrar is None:
        return
    resultado = cerrar()
    if inspect.isawaitable(resultado):
        await resultado
