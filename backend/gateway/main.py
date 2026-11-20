"""Entry point del gateway.

El contenedor corre `python -m service.main`. El gateway es un servidor
**HTTP** (FastAPI + uvicorn), no gRPC: consume gRPC de los servicios
internos y habla HTTP con el BFF. El puerto sale de `GATEWAY_PORT`
(8080 por defecto) y el healthcheck del compose pide `GET /healthz`.
"""

from __future__ import annotations

import logging

import uvicorn

from common.logging import setup_logging

from .app import crear_app
from .config import ConfiguracionGateway

logger = logging.getLogger("gateway.main")


def main() -> None:
    """Arranca el servidor HTTP del gateway."""
    config = ConfiguracionGateway.from_env()
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
