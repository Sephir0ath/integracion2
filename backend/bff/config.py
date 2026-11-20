"""Configuracion del BFF.

`common.config.Settings` cubre todo lo que el BFF comparte con el resto
del sistema: el limite de tiempo de las llamadas internas y los destinos de
los servicios de dominio.

Lo unico que el BFF necesita y `Settings` no tiene son el puerto de
escucha y el directorio de la interfaz web, asi que se leen del entorno
aqui (la biblioteca comun es de solo lectura).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from common.config import Settings, get_settings

# Puerto de escucha del BFF.
PUERTO_POR_DEFECTO = 8081

# Directorio de la interfaz web dentro de la imagen. El Dockerfile copia
# `frontend/web/` ahi solo cuando el servicio que se construye es el BFF.
DIRECTORIO_INTERFAZ_POR_DEFECTO = "/app/frontend/web"


def _puerto(valor: str | None) -> int:
    """Puerto de escucha, con el valor por defecto si el entorno no dice."""
    try:
        return int(valor) if valor else PUERTO_POR_DEFECTO
    except ValueError:
        return PUERTO_POR_DEFECTO


@dataclass(frozen=True)
class ConfiguracionBff:
    """Todo lo que el BFF necesita para arrancar."""

    puerto: int = PUERTO_POR_DEFECTO
    directorio_interfaz: str = DIRECTORIO_INTERFAZ_POR_DEFECTO
    settings: Settings = field(default_factory=Settings)

    @classmethod
    def from_env(cls) -> "ConfiguracionBff":
        """Configuracion leida del entorno."""
        return cls(
            puerto=_puerto(os.getenv("BFF_PORT")),
            directorio_interfaz=os.getenv("BFF_INTERFAZ_DIR", DIRECTORIO_INTERFAZ_POR_DEFECTO),
            settings=get_settings(),
        )
