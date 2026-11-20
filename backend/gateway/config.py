"""Configuracion del gateway.

`common.config.Settings` cubre todo lo que el gateway comparte con el resto
del sistema: el secreto y el algoritmo del token, su vida, los limites de
peticiones, el limite de tiempo de las llamadas internas, los destinos de
los servicios de dominio y la URL del BFF.

Lo unico que el gateway necesita y `Settings` no tiene son el puerto de
escucha y la lista de usuarios, asi que se leen del entorno aqui (la
biblioteca comun es de solo lectura).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from common.config import Settings, get_settings

from .usuarios import Usuario, usuarios_desde_entorno

# Puerto de escucha del gateway.
PUERTO_POR_DEFECTO = 8080


def _puerto(valor: str | None) -> int:
    """Puerto de escucha, con el valor por defecto si el entorno no dice."""
    try:
        return int(valor) if valor else PUERTO_POR_DEFECTO
    except ValueError:
        return PUERTO_POR_DEFECTO


@dataclass(frozen=True)
class ConfiguracionGateway:
    """Todo lo que el gateway necesita para arrancar."""

    puerto: int = PUERTO_POR_DEFECTO
    usuarios: tuple[Usuario, ...] = field(default_factory=tuple)
    settings: Settings = field(default_factory=Settings)

    @classmethod
    def from_env(cls) -> "ConfiguracionGateway":
        """Configuracion leida del entorno."""
        return cls(
            puerto=_puerto(os.getenv("GATEWAY_PORT")),
            usuarios=usuarios_desde_entorno(os.getenv("GATEWAY_USERS")),
            settings=get_settings(),
        )
