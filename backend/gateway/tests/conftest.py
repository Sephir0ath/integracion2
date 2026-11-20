"""Fixtures de las pruebas del gateway.

La suite corre sin Redis y sin los servicios internos levantados: las tres
dependencias externas entran por los seams del constructor de la aplicacion
(el stub gRPC, el cliente de Redis y el cliente HTTP del BFF) y se sustituyen
por los dobles de `tests/fakes.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest

from common.config import Settings
from common.metrics import REGISTRY

from ..app import crear_app
from ..auth import emitir_token
from ..clients import ClienteBff, ClienteButacas, ClienteCompras, ClienteEmision
from ..config import ConfiguracionGateway
from ..rate_limit import LimitadorTazon
from ..usuarios import USUARIOS_DEMOSTRACION, AlmacenUsuarios
from .fakes import FakeButacasStub, FakeComprasStub, FakeEmisionStub, FakeRedis


def valor_contador(nombre: str, etiquetas: dict | None = None) -> float:
    """Valor actual de un contador del registro de `common.metrics`."""
    return REGISTRY.get_sample_value(nombre, etiquetas) or 0.0


@asynccontextmanager
async def cliente_para(app):
    """Cliente HTTP contra una aplicacion del gateway, en memoria."""
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://gateway") as cliente:
        yield cliente


@pytest.fixture()
def settings() -> Settings:
    """Configuracion de la prueba: todo local y sin dependencias reales."""
    return Settings(
        service_name="gateway",
        jwt_secret="secreto-de-pruebas-del-gateway-u3-2026",
        jwt_algorithm="HS256",
        jwt_ttl_seconds=3600,
        rate_limit_per_minute=60,
        rate_limit_burst=20,
        grpc_deadline=1.5,
        butacas_target="butacas:50051",
        compras_target="compras:50054",
        emision_target="emision:50053",
        bff_url="http://bff:8081",
    )


@pytest.fixture()
def almacen() -> AlmacenUsuarios:
    """Almacen de usuarios con el juego de demostracion."""
    return AlmacenUsuarios(USUARIOS_DEMOSTRACION)


def _usuario(rol: str):
    """Usuario de demostracion con el rol pedido."""
    for usuario in USUARIOS_DEMOSTRACION:
        if usuario.role == rol:
            return usuario
    raise AssertionError(f"no hay usuario de demostracion con rol {rol}")


@pytest.fixture()
def tokens(settings):
    """Emisor de tokens de prueba."""

    class Emisor:
        """Monta tokens con los claims del contrato."""

        def admin(self) -> str:
            """Token del usuario de demostracion `admin`."""
            token, _ = emitir_token(_usuario("admin"), settings)
            return token

        def cliente(self) -> str:
            """Token del usuario de demostracion `cliente`."""
            token, _ = emitir_token(_usuario("cliente"), settings)
            return token

    return Emisor()


@pytest.fixture()
def stubs():
    """Dobles de los tres servicios internos."""
    return {
        "butacas": FakeButacasStub(),
        "compras": FakeComprasStub(),
        "emision": FakeEmisionStub(),
    }


@pytest.fixture()
def clientes(settings, stubs):
    """Clientes gRPC del gateway con los stubs falsos inyectados."""
    return {
        "butacas": ClienteButacas(settings, stub=stubs["butacas"]),
        "compras": ClienteCompras(settings, stub=stubs["compras"]),
        "emision": ClienteEmision(settings, stub=stubs["emision"]),
    }


@pytest.fixture()
def limitador() -> LimitadorTazon:
    """Limitador sin Redis: el respaldo en memoria."""
    return LimitadorTazon(redis=None, capacidad=20, recarga_por_minuto=60)


@pytest.fixture()
def bff_registro():
    """Registro de lo que el gateway le pide al BFF."""
    return {
        "peticiones": [],
        "respuesta": (200, b'{"eventos":[],"degradado":false}', "application/json"),
    }


@pytest.fixture()
async def cliente_bff(settings, bff_registro):
    """Cliente HTTP del BFF con un transporte simulado."""

    def manejador(peticion: httpx.Request) -> httpx.Response:
        bff_registro["peticiones"].append(peticion)
        estado, cuerpo, _ = bff_registro["respuesta"]
        return httpx.Response(estado, content=cuerpo, headers={"content-type": "application/json"})

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(manejador))
    try:
        yield ClienteBff(settings, cliente=cliente)
    finally:
        await cliente.aclose()


@pytest.fixture()
def config(settings) -> ConfiguracionGateway:
    """Configuracion del gateway para la prueba."""
    return ConfiguracionGateway(
        puerto=8080,
        usuarios=USUARIOS_DEMOSTRACION,
        settings=settings,
    )


@pytest.fixture()
def construir_app(config, almacen, clientes, limitador, cliente_bff):
    """Fabrica de la aplicacion: permite cambiar una dependencia."""

    def construir(**sobrescrituras):
        dependencias = {
            "almacen": almacen,
            "butacas": clientes["butacas"],
            "compras": clientes["compras"],
            "emision": clientes["emision"],
            "limitador": limitador,
            "cliente_bff": cliente_bff,
        }
        dependencias.update(sobrescrituras)
        return crear_app(config, **dependencias)

    return construir


@pytest.fixture()
def app(construir_app):
    """Aplicacion del gateway con todas las dependencias inyectadas."""
    return construir_app()


@pytest.fixture()
async def http(app):
    """Cliente HTTP contra la aplicacion, en memoria."""
    async with cliente_para(app) as cliente:
        yield cliente


@pytest.fixture()
def redis_caido() -> FakeRedis:
    """Redis que no responde."""
    return FakeRedis(error=RuntimeError("redis no responde"))
