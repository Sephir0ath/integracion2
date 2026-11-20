"""Fixtures de las pruebas del BFF.

La suite corre **sin los servicios internos levantados**: los cuatro
clientes gRPC entran por los seams que deja el constructor de la aplicacion
y se sustituyen por los dobles de `tests/fakes.py`. La interfaz web se
sirve desde un directorio de pruebas, asi que tampoco hace falta el
contenedor con el front.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from common.config import Settings

from ..app import crear_app
from ..clients import ClienteButacas, ClienteCompras, ClienteEmision, ClientePagos
from ..config import ConfiguracionBff
from .fakes import FakeButacasStub, FakeComprasStub, FakeEmisionStub, FakePagosStub


@asynccontextmanager
async def cliente_para(app):
    """Cliente HTTP contra una aplicacion del BFF, en memoria."""
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://bff") as cliente:
        yield cliente


@pytest.fixture()
def settings() -> Settings:
    """Configuracion de la prueba: todo local y sin dependencias reales."""
    return Settings(
        service_name="bff",
        grpc_deadline=1.5,
        butacas_target="butacas:50051",
        pagos_target="pagos:50052",
        emision_target="emision:50053",
        compras_target="compras:50054",
        bff_url="http://bff:8081",
    )


@pytest.fixture()
def stubs():
    """Dobles de los cuatro servicios internos."""
    return {
        "butacas": FakeButacasStub(),
        "pagos": FakePagosStub(),
        "emision": FakeEmisionStub(),
        "compras": FakeComprasStub(),
    }


@pytest.fixture()
def clientes(settings, stubs):
    """Clientes gRPC del BFF con los stubs falsos inyectados."""
    return {
        "butacas": ClienteButacas(settings, stub=stubs["butacas"]),
        "pagos": ClientePagos(settings, stub=stubs["pagos"]),
        "emision": ClienteEmision(settings, stub=stubs["emision"]),
        "compras": ClienteCompras(settings, stub=stubs["compras"]),
    }


@pytest.fixture()
def interfaz(tmp_path: Path) -> Path:
    """Directorio de pruebas con la interfaz web estatica."""
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><head><title>Ticketera</title></head>"
        "<body><h1>Ticketera</h1><link rel='stylesheet' href='/estilos.css'></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "estilos.css").write_text("body { color: #14303A; }", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def config(settings, interfaz) -> ConfiguracionBff:
    """Configuracion del BFF para la prueba."""
    return ConfiguracionBff(
        puerto=8081,
        directorio_interfaz=str(interfaz),
        settings=settings,
    )


@pytest.fixture()
def construir_app(config, clientes):
    """Fabrica de la aplicacion: permite cambiar una dependencia."""

    def construir(**sobrescrituras):
        dependencias = {
            "butacas": clientes["butacas"],
            "pagos": clientes["pagos"],
            "emision": clientes["emision"],
            "compras": clientes["compras"],
        }
        dependencias.update(sobrescrituras)
        return crear_app(config, **dependencias)

    return construir


@pytest.fixture()
def app(construir_app):
    """Aplicacion del BFF con todas las dependencias inyectadas."""
    return construir_app()


@pytest.fixture()
async def http(app):
    """Cliente HTTP contra la aplicacion, en memoria."""
    async with cliente_para(app) as cliente:
        yield cliente
