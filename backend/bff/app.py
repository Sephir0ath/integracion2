"""Aplicacion HTTP del BFF.

El BFF es la capa **a medida del cliente**: compone en una sola respuesta
los datos de dos o mas servicios y sirve la interfaz web estatica que ya
esta construida.

Tabla de rutas (el contrato del borde):

| Metodo + ruta                                  | Que compone                          |
|------------------------------------------------|--------------------------------------|
| `GET /healthz`                                 | liveness del BFF (sin dependencias)  |
| `GET /metrics`                                 | texto Prometheus del registro comun  |
| `GET /`                                        | la interfaz web estatica             |
| `GET /bff/web/catalogo`                        | **Butacas + Pagos**                  |
| `GET /bff/web/secciones/{id}/butacas`          | **Butacas** (plano de la sala)       |
| `POST /bff/web/compras`                        | **Compras + Emision**                |
| `GET /bff/web/compras/{saga_id}`               | **Compras + Butacas + Emision**      |

El BFF **no revalida el JWT**: el gateway ya lo valido y reenvia la
identidad del cliente en `X-Client-Id` y `X-Role`. Las rutas de negocio
exigen `X-Client-Id`: su ausencia significa que la peticion no vino por la
puerta de entrada, y la respuesta es 401.

Lo que distingue al BFF de un proxy es la **degradacion elegante**: una
dependencia opcional que falla no rompe la respuesta (se contesta 200 con
lo que hay y marcada como degradada); una dependencia esencial que falla
se traduce a 503 con el codigo de `common.errors`, nunca a un 500 opaco.

Las cuatro dependencias externas entran por el constructor (seams
inyectables): asi la suite corre sin los servicios internos levantados.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ValidationError

from common.errors import CODES, ServiceError
from common.logging import log_event, set_request_id
from common.metrics import REGISTRY

from .clients import ClienteButacas, ClienteCompras, ClienteEmision, ClientePagos
from .config import ConfiguracionBff
from .errores import CODIGO_INVALIDO, CODIGO_NO_AUTENTICADO, ErrorBff
from .metricas import duracion, solicitudes

logger = logging.getLogger("bff.app")


# ---------------------------------------------------------------- cuerpos --
class CuerpoCompra(BaseModel):
    """Cuerpo de `POST /bff/web/compras`.

    El `cliente_id` no viene del cuerpo: lo pone el BFF desde la cabecera
    `X-Client-Id` que reenvia el gateway, porque el cliente no puede
    comprar en nombre de otro. La `moneda` es opcional: si no viene, manda
    la del dominio.
    """

    saga_id: str = ""
    evento_id: str = ""
    seccion_id: str = ""
    butaca_ids: list[str] = []
    moneda: str = "COP"


# -------------------------------------------------------------- aplicacion --
def crear_app(
    config: ConfiguracionBff | None = None,
    *,
    butacas: ClienteButacas | None = None,
    pagos: ClientePagos | None = None,
    emision: ClienteEmision | None = None,
    compras: ClienteCompras | None = None,
) -> FastAPI:
    """Construye la aplicacion del BFF.

    Las cuatro dependencias externas se pueden inyectar: sin inyeccion se
    usan las reales (gRPC contra los servicios internos).
    """
    config = config or ConfiguracionBff.from_env()
    settings = config.settings

    butacas = butacas or ClienteButacas(settings)
    pagos = pagos or ClientePagos(settings)
    emision = emision or ClienteEmision(settings)
    compras = compras or ClienteCompras(settings)

    @asynccontextmanager
    async def _vida(_app):
        """Abre y cierra lo que el proceso abrio."""
        log_event(
            logger,
            "bff_iniciando",
            puerto=config.puerto,
            interfaz=config.directorio_interfaz,
            butacas=settings.butacas_target,
            pagos=settings.pagos_target,
            emision=settings.emision_target,
            compras=settings.compras_target,
            deadline=settings.grpc_deadline,
        )
        try:
            yield
        finally:
            for cliente in (butacas, pagos, emision, compras):
                await cliente.close()

    app = FastAPI(
        title="BFF de la ticketera",
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
    @app.exception_handler(ErrorBff)
    async def _error_del_borde(request: Request, error: ErrorBff):
        """Los errores del BFF, con su codigo."""
        return JSONResponse(
            {"code": error.codigo, "message": error.mensaje},
            status_code=error.estado,
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

    @app.exception_handler(Exception)
    async def _error_inesperado(request: Request, error: Exception):
        """Red de seguridad: un fallo raro nunca sale como un 500 opaco.

        Los fallos de las dependencias ya llegan traducidos a
        `ServiceError`; esta mano evita que un error no previsto en el
        borde parezca una caida del sistema sin codigo.
        """
        log_event(
            logger,
            "bff_error_inesperado",
            error=type(error).__name__,
            detalle=str(error),
            ruta=str(request.url.path),
        )
        return JSONResponse(
            {
                "code": CODES.DEPENDENCY_UNAVAILABLE,
                "message": f"el BFF no pudo atender la peticion: {error}",
            },
            status_code=503,
        )

    # -------------------------------------------------------- dependencias --
    async def _identidad(
        x_client_id: str | None = Header(default=None),
        x_role: str | None = Header(default=None),
    ) -> dict:
        """La identidad del cliente, tal como la valido el gateway.

        El BFF no vuelve a validar el token: confia en las cabeceras que
        el gateway reenvio. Si no hay `X-Client-Id`, la peticion no vino
        por la puerta de entrada y se contesta 401.
        """
        cliente = (x_client_id or "").strip()
        if not cliente:
            raise ErrorBff(
                401,
                CODIGO_NO_AUTENTICADO,
                "la peticion no viene por la puerta de entrada: falta X-Client-Id",
            )
        return {"x-client-id": cliente, "x-role": (x_role or "").strip()}

    # -------------------------------------------------------------- publico --
    @app.get("/healthz")
    async def healthz():
        """Liveness del BFF.

        No consulta ninguna dependencia: el BFF tiene que poder decir que
        esta vivo precisamente cuando el resto del sistema esta caido.
        """
        return {
            "status": "ok",
            "service": settings.service_name,
            "interfaz": config.directorio_interfaz,
            "interfaz_servida": _interfaz_servida(config),
        }

    @app.get("/metrics")
    async def metrics():
        """El texto Prometheus del registro compartido."""
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    # -------------------------------------------------------------- catalogo --
    @app.get("/bff/web/catalogo")
    async def catalogo(identidad: dict = Depends(_identidad)):
        """El catalogo del recinto: **butacas** (eventos y secciones) y
        **pagos** (metodos de pago) en un solo cuerpo.

        `butacas` es esencial: si cae, 503. `pagos` es opcional: si cae,
        la respuesta sigue siendo 200, sin `metodos_pago` y marcada como
        degradada.
        """
        metadatos = _metadatos(identidad)
        eventos = await butacas.listar_eventos(metadatos=metadatos)
        secciones = await butacas.listar_secciones(metadatos=metadatos)
        metodos_pago, degradado = await _dependencia_opcional(
            lambda: pagos.listar_metodos_pago(metadatos=metadatos),
            "pagos",
            ruta="/bff/web/catalogo",
        )
        respuesta: dict = {"eventos": eventos, "secciones": secciones}
        if not degradado:
            respuesta["metodos_pago"] = metodos_pago
        respuesta["pagos_disponible"] = not degradado
        respuesta["degradado"] = degradado
        return respuesta

    # ---------------------------------------------------------- plano de sala --
    @app.get("/bff/web/secciones/{seccion_id}/butacas")
    async def plano_de_la_sala(seccion_id: str, identidad: dict = Depends(_identidad)):
        """El plano de la sala: las butacas de una seccion con su estado."""
        plano = await butacas.listar_butacas(seccion_id, metadatos=_metadatos(identidad))
        return {"seccion_id": seccion_id, "butacas": plano}

    # -------------------------------------------------------------- compras --
    @app.post("/bff/web/compras")
    async def iniciar_compra(request: Request, identidad: dict = Depends(_identidad)):
        """Inicia la compra: **compras** (la saga) y **emision** (las
        entradas) en un solo cuerpo.

        `compras` es esencial: si cae, 503. `emision` es opcional: si cae,
        la respuesta trae la compra y las entradas vacias.
        """
        cuerpo = await _cuerpo_de_compra(request)
        saga_id = (cuerpo.saga_id or "").strip() or uuid.uuid4().hex
        elegidas = [str(b).strip() for b in (cuerpo.butaca_ids or []) if str(b).strip()]
        if not elegidas:
            raise ServiceError(
                CODES.INVALID,
                "hay que elegir al menos una butaca para comprar",
            )
        metadatos = _metadatos(identidad)
        compra = await compras.iniciar_compra(
            saga_id,
            cuerpo.evento_id,
            cuerpo.seccion_id,
            elegidas,
            identidad["x-client-id"],
            cuerpo.moneda or "COP",
            metadatos,
        )
        entradas, _degradado = await _dependencia_opcional(
            lambda: emision.listar_entradas(saga_id, metadatos=metadatos),
            "emision",
            saga_id=saga_id,
        )
        return {"compra": compra, "entradas": entradas or []}

    @app.get("/bff/web/compras/{saga_id}")
    async def consultar_compra(saga_id: str, identidad: dict = Depends(_identidad)):
        """El estado de una saga: **compras**, **butacas** (la reserva) y
        **emision** (las entradas) en un solo cuerpo.

        `compras` es esencial: si cae, 503. Las otras dos son opcionales:
        si caen, la respuesta es 200 y degradada.
        """
        metadatos = _metadatos(identidad)
        compra = await compras.consultar_compra(saga_id, metadatos=metadatos)
        reserva, degrada_reserva = await _dependencia_opcional(
            lambda: butacas.consultar_reserva(saga_id, metadatos=metadatos),
            "butacas",
            saga_id=saga_id,
        )
        entradas, degrada_entradas = await _dependencia_opcional(
            lambda: emision.listar_entradas(saga_id, metadatos=metadatos),
            "emision",
            saga_id=saga_id,
        )
        return {
            "compra": compra,
            "reserva": reserva,
            "entradas": entradas or [],
            "degradado": degrada_reserva or degrada_entradas,
        }

    # ------------------------------------------------------- interfaz web --
    # El montaje va al final: las rutas de arriba se resuelven primero y la
    # interfaz solo atiende lo que queda (los archivos estaticos).
    directorio = Path(config.directorio_interfaz)
    if directorio.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(directorio), html=True),
            name="interfaz",
        )
    else:
        log_event(
            logger,
            "bff_interfaz_no_encontrada",
            directorio=str(directorio),
        )

    return app


# ------------------------------------------------------------- auxiliares --
async def _cuerpo_de_compra(request: Request) -> CuerpoCompra:
    """El cuerpo de la compra, leido a mano.

    El BFF **no exige** `Content-Type: application/json`: el gateway
    reenvia el cuerpo de la peticion pero no su tipo de contenido, asi que
    una compra que llega por el gateway llega sin esa cabecera. El cuerpo
    se lee como JSON sea cual sea su tipo; un cuerpo que no es un objeto
    JSON es un 400 `INVALID`.
    """
    crudo = await request.body()
    if not crudo or not crudo.strip():
        return CuerpoCompra()
    try:
        datos = json.loads(crudo)
    except (TypeError, ValueError) as error:
        raise ServiceError(
            CODES.INVALID,
            "el cuerpo de la compra no es un JSON valido",
        ) from error
    if not isinstance(datos, dict):
        raise ServiceError(
            CODES.INVALID,
            "el cuerpo de la compra debe ser un objeto JSON",
        )
    try:
        return CuerpoCompra(**datos)
    except ValidationError as error:
        raise ServiceError(
            CODES.INVALID,
            "el cuerpo de la compra no cumple el contrato",
        ) from error


async def _dependencia_opcional(operacion, dependencia: str, **campos):
    """Ejecuta una dependencia opcional sin romper la respuesta.

    Devuelve ``(valor, degradado)``. Si la dependencia falla, el error no
    se propaga: la respuesta se arma con lo que hay y se marca la
    degradacion, que es justo lo que distingue al BFF de un proxy.
    """
    try:
        return await operacion(), False
    except ServiceError as error:
        log_event(
            logger,
            "bff_degradado",
            dependencia=dependencia,
            codigo=error.code,
            mensaje=error.message,
            **campos,
        )
        return None, True
    except Exception as error:  # noqa: BLE001 - una dependencia caida no rompe la respuesta
        log_event(
            logger,
            "bff_degradado",
            dependencia=dependencia,
            codigo=type(error).__name__,
            mensaje=str(error),
            **campos,
        )
        return None, True


def _metadatos(identidad: dict) -> tuple:
    """Las metadatos de identidad que viajan al servicio interno."""
    return tuple((clave, valor) for clave, valor in identidad.items() if valor)


def _interfaz_servida(config: ConfiguracionBff) -> bool:
    """Si el directorio de la interfaz web esta disponible."""
    return Path(config.directorio_interfaz).is_dir()


def _contar(request: Request, estado: int, segundos: float) -> None:
    """Cuenta la peticion en las metricas del BFF."""
    ruta = _plantilla_de_la_ruta(request)
    solicitudes.labels(ruta, request.method, str(estado)).inc()
    duracion.labels(ruta).observe(segundos)


def _plantilla_de_la_ruta(request: Request) -> str:
    """La plantilla de la ruta (`/bff/web/compras/{saga_id}`).

    Se usa la plantilla y no la ruta concreta para que las series no
    crezcan con cada identificacion de saga o de butaca. La interfaz web
    se sirve con un montaje en la raiz: sus archivos se agrupan bajo
    `/interfaz` en vez de aparecer como peticiones sin ruta conocida.
    """
    ruta = getattr(request.scope.get("route"), "path", None)
    if ruta:
        return str(ruta)
    if isinstance(request.scope.get("endpoint"), StaticFiles):
        return "/interfaz"
    return "sin_coincidencia"


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
