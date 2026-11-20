"""Entry point of the `compras` service.

The container runs `python -m service.main`; the server is started inside
the `if __name__ == "__main__":` block so the module can be imported by
the tests without starting anything.

The three clients of the dependencies are built **inside** the event loop
of the server: a channel of `grpc.aio` belongs to the loop that opens it,
so building them outside would bind the saga to the wrong loop.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select

from common.config import Settings, get_settings
from common.db import Base, create_engine, init_schema, session_scope, wait_for_database
from common.grpc import serve_grpc
from common.logging import log_event, setup_logging

from .clients import ButacasClient, EmisionClient, PagosClient
from .grpc_service import ComprasServicer, registrar
from .models import Saga
from .service import ComprasService

logger = logging.getLogger("compras.main")

# Used only when the service runs without a database (development sandbox).
# In the system the variable DATABASE_URL always points to the own database
# of the service.
URL_SQLITE_DEVELOPMENT = "sqlite:////tmp/compras-desarrollo.db"


def build_engine(settings: Settings):
    """Build the engine of the own database of the service."""
    url = settings.database_url
    opciones = {}
    if not url:
        url = URL_SQLITE_DEVELOPMENT
        log_event(
            logger,
            "compras_db_sin_configurar",
            url=url,
            motivo="DATABASE_URL no esta definido",
        )
    if url.startswith("sqlite"):
        # El motor de desarrollo tiene que aguantar llamadas concurrentes.
        opciones["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **opciones)
    if url.startswith("sqlite"):
        _activar_wal(engine)
    return engine


def _activar_wal(engine) -> None:
    """Lectores y escritores a la vez en el motor SQLite de desarrollo."""

    @sqlalchemy_event.listens_for(engine, "connect")
    def _configurar(dbapi_connection, connection_record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def build_service(settings: Settings, engine, butacas, pagos, emision) -> ComprasService:
    """Build the orchestrator over the session provider of the engine."""
    return ComprasService(
        lambda: session_scope(engine),
        butacas,
        pagos,
        emision,
        deadline=settings.grpc_deadline,
    )


def main() -> None:
    """Start the gRPC server and the admin HTTP app of the service."""
    settings = get_settings()
    setup_logging(settings.service_name)

    engine = build_engine(settings)
    wait_for_database(engine)
    init_schema(engine, Base)

    log_event(
        logger,
        "compras_iniciando",
        grpc_port=settings.grpc_port,
        admin_port=settings.admin_port,
        butacas=settings.butacas_target,
        pagos=settings.pagos_target,
        emision=settings.emision_target,
        deadline=settings.grpc_deadline,
    )

    asyncio.run(_servir(settings, engine))


async def _servir(settings: Settings, engine) -> None:
    """Run the gRPC server and the admin HTTP app of the service."""

    # Los canales se abren en este loop, que es el loop del servidor.
    butacas = ButacasClient(settings)
    pagos = PagosClient(settings)
    emision = EmisionClient(settings)

    servicio = build_service(settings, engine, butacas, pagos, emision)
    servicer = ComprasServicer(servicio)

    def registrar_servidor(server):
        registrar(server, servicer)

    def health():
        with session_scope(engine) as session:
            sagas = session.execute(select(func.count()).select_from(Saga)).scalar_one()
        return {
            "sagas": int(sagas),
            "butacas": settings.butacas_target,
            "pagos": settings.pagos_target,
            "emision": settings.emision_target,
            "deadline": settings.grpc_deadline,
        }

    try:
        await serve_grpc(
            settings.service_name,
            settings.grpc_port,
            settings.admin_port,
            registrar_servidor,
            health,
        )
    finally:
        for cliente in (butacas, pagos, emision):
            await cliente.close()


if __name__ == "__main__":
    main()
