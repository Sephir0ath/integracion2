"""Errores del borde.

El gateway responde siempre con la misma forma de error:

    {"code": ..., "message": ...}

Los codigos de dominio son los de `common.errors` (`NOT_FOUND`,
`DEPENDENCY_UNAVAILABLE`...). Los del borde son propios del gateway,
porque no son fallos de un servicio de dominio sino de la puerta misma:
autenticacion, autorizacion y limite de peticiones.
"""

from __future__ import annotations

from common.errors import CODES

# Codigos del borde.
CODIGO_NO_AUTENTICADO = "NO_AUTENTICADO"
CODIGO_SIN_PERMISO = "SIN_PERMISO"
CODIGO_LIMITE = "LIMITE_DE_PETICIONES"

# El cuerpo que no cumple el contrato es un error de peticion invalida.
CODIGO_INVALIDO = CODES.INVALID


class ErrorGateway(Exception):
    """Error del gateway, con su estado HTTP, su codigo y sus cabeceras."""

    def __init__(
        self,
        estado: int,
        codigo: str,
        mensaje: str,
        cabeceras: dict | None = None,
    ) -> None:
        super().__init__(mensaje)
        self.estado = estado
        self.codigo = codigo
        self.mensaje = mensaje
        self.cabeceras = cabeceras or {}
