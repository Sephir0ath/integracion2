"""Entry point del BFF.

El contenedor corre `python -m service.main`. El BFF es un servidor
**HTTP** (FastAPI + uvicorn): habla gRPC con los servicios internos y
sirve la interfaz web estatica. El puerto sale de `BFF_PORT` (8081 por
defecto) y el healthcheck del compose pide `GET /healthz`.
"""

from __future__ import annotations

import logging

import uvicorn

from common.logging import setup_logging

from .app import crear_app
from .config import ConfiguracionBff

logger = logging.getLogger("bff.main")


def main() -> None:
    """Arranca el servidor HTTP del BFF."""
    config = ConfiguracionBff.from_env()
    setup_logging(config.settings.service_name)
    app = crear_app(config)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=config.puerto,
        log_level=config.settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
