"""Entry point of the `emision` service.

The container runs `python -m service.main`; the server is started inside
the `if __name__ == "__main__":` block so the module can be imported by
the tests without starting anything.
"""

from __future__ import annotations

import asyncio
import logging

import common.grpc as common_grpc
from sqlalchemy import func, select
from sqlalchemy import event as sqlalchemy_event

from common.config import Settings, get_settings
from common.db import Base, create_engine, init_schema, session_scope, wait_for_database
from common.grpc import serve_grpc
from common.logging import log_event, setup_logging

from .faults import FaultController, registrar_rutas
from .grpc_service import EmisionServicer, registrar
from .models import Entrada
from .pagos_client import PagosClient
from .service import EmisionService

logger = logging.getLogger("emision.main")

# Used only when the service runs without a database (development sandbox).
# In the system the variable DATABASE_URL always points to the own
# database of the service.
URL_SQLITE_DEVELOPMENT = "sqlite:////tmp/emision-desarrollo.db"

# Control plane of the faults (used by the demo scripts).
FALLAS = FaultController()

_rutas_de_falla_instaladas = False


def build_engine(settings: Settings):
    """Build the engine of the own database of the service."""
    url = settings.database_url
    opciones = {}
    if not url:
        url = URL_SQLITE_DEVELOPMENT
        log_event(
            logger,
            "emision_db_sin_configurar",
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


def _instalar_rutas_de_falla() -> None:
    """Add the control plane of the faults to the admin application.

    `common.grpc.serve_grpc` builds the admin application with
    `common.grpc.build_admin_app`. The shared contract also asks `emision`
    to expose `POST /admin/fault` (the control plane of the demo scripts,
    used to force the last step of the saga to fail), so the application
    of the shared library is extended here instead of forking it.
    """
    global _rutas_de_falla_instaladas
    if _rutas_de_falla_instaladas:
        return

    original = common_grpc.build_admin_app

    def build_admin_app(service_name, health_fn=None):
        app = original(service_name, health_fn)
        registrar_rutas(app, FALLAS)
        return app

    common_grpc.build_admin_app = build_admin_app
    _rutas_de_falla_instaladas = True


def main() -> None:
    """Start the gRPC server and the admin HTTP app of the service."""
    settings = get_settings()
    setup_logging(settings.service_name)

    engine = build_engine(settings)
    wait_for_database(engine)
    init_schema(engine, Base)

    _instalar_rutas_de_falla()

    log_event(
        logger,
        "emision_iniciando",
        grpc_port=settings.grpc_port,
        admin_port=settings.admin_port,
        pagos=settings.pagos_target,
        deadline=settings.grpc_deadline,
    )

    asyncio.run(_servir(settings, engine))


async def _servir(settings: Settings, engine) -> None:
    """Run the gRPC server and the admin HTTP app of the service.

    The client of `pagos` is built here, inside the event loop of the
    server: the channel of `grpc.aio` belongs to the loop that opens it.
    """
    pagos = PagosClient(settings)
    servicio = EmisionService(lambda: session_scope(engine), pagos, faults=FALLAS)
    servicer = EmisionServicer(servicio)

    def registrar_servidor(server):
        registrar(server, servicer)

    def health():
        with session_scope(engine) as session:
            entradas = session.execute(select(func.count()).select_from(Entrada)).scalar_one()
        return {
            "entradas": int(entradas),
            "pagos": settings.pagos_target,
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
        await pagos.close()


if __name__ == "__main__":
    main()
