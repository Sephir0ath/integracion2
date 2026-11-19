"""Entry point of the simulated payment gateway.

The container runs `python -m service.main`; the server is started inside
the `if __name__ == "__main__":` block so the module can be imported by the
tests without starting anything.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from common.config import get_settings
from common.logging import log_event, setup_logging

from .app import create_app

logger = logging.getLogger("pasarela")

# The shared settings do not carry the port of the simulator (it is not a
# domain service), so it is read from the environment directly.
PASARELA_PORT_POR_DEFECTO = 9500


def puerto() -> int:
    """Port the simulator listens on."""
    try:
        return int(os.getenv("PASARELA_PORT", str(PASARELA_PORT_POR_DEFECTO)))
    except ValueError:
        return PASARELA_PORT_POR_DEFECTO


def main() -> None:
    """Start the HTTP server of the simulator."""
    settings = get_settings()
    setup_logging(settings.service_name)

    puerto_actual = puerto()
    log_event(logger, "pasarela_iniciando", puerto=puerto_actual)

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=puerto_actual,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
