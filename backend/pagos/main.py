"""Entry point of the `pagos` service.

The container runs `python -m service.main`; the server is started inside
the `if __name__ == "__main__":` block so the module can be imported by the
tests without starting anything.
"""

from __future__ import annotations

import asyncio
import logging

from common.config import Settings, get_settings
from common.db import Base, create_engine, init_schema, session_scope, wait_for_database
from common.grpc import serve_grpc
from common.logging import log_event, setup_logging
from sqlalchemy import event

from .grpc_service import PagosServicer, registrar
from .pasarela import PasarelaClient
from .service import PagosService

logger = logging.getLogger("pagos.main")

# Used only when the service runs without a database (development sandbox
# and the functional proof of the resilience patterns). In the system the
# variable DATABASE_URL always points to the own database of the service.
URL_SQLITE_DEVELOPMENT = "sqlite:////tmp/pagos-desarrollo.db"


def build_engine(settings: Settings):
    """Build the engine of the own database of the service."""
    url = settings.database_url
    opciones = {}
    if not url:
        url = URL_SQLITE_DEVELOPMENT
        log_event(
            logger,
            "pagos_db_sin_configurar",
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
    @event.listens_for(engine, "connect")
    def _configurar(dbapi_connection, connection_record):  # pragma: no cover
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


def build_service(settings: Settings):
    """Build the engine and the domain service."""
    engine = build_engine(settings)
    pasarela = PasarelaClient(settings)
    servicio = PagosService(lambda: session_scope(engine), pasarela)
    return engine, servicio, pasarela


def main() -> None:
    """Start the gRPC server and the admin HTTP app of the service."""
    settings = get_settings()
    setup_logging(settings.service_name)

    engine, servicio, pasarela = build_service(settings)
    wait_for_database(engine)
    init_schema(engine, Base)

    servicer = PagosServicer(servicio)

    def registrar_servidor(server):
        registrar(server, servicer)

    def health():
        estado = pasarela.snapshot()
        return {
            "circuito": estado["estado"],
            "degradacion": estado["fallback"],
            "pasarela": settings.pasarela_url,
        }

    log_event(
        logger,
        "pagos_iniciando",
        grpc_port=settings.grpc_port,
        admin_port=settings.admin_port,
        pasarela=settings.pasarela_url,
    )

    try:
        asyncio.run(
            serve_grpc(
                settings.service_name,
                settings.grpc_port,
                settings.admin_port,
                registrar_servidor,
                health,
            )
        )
    finally:
        asyncio.run(pasarela.close())


if __name__ == "__main__":
    main()
