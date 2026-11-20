"""Errores del BFF.

El BFF responde con la misma forma de error que el resto del sistema:

    {"code": ..., "message": ...}

Los codigos de dominio son los de `common.errors` (`NOT_FOUND`,
`DEPENDENCY_UNAVAILABLE`, `INVALID`...). El unico codigo propio es el de la
peticion que **no vino por la puerta de entrada**: sin `X-Client-Id` el
BFF no sabe quien compra, asi que contesta 401.
"""

from __future__ import annotations

from common.errors import CODES

# La peticion no trae la identidad que el gateway valido.
CODIGO_NO_AUTENTICADO = "NO_AUTENTICADO"

# El cuerpo no cumple el contrato (por ejemplo, una compra sin butacas).
CODIGO_INVALIDO = CODES.INVALID


class ErrorBff(Exception):
    """Error del BFF, con su estado HTTP y su codigo."""

    def __init__(self, estado: int, codigo: str, mensaje: str) -> None:
        super().__init__(mensaje)
        self.estado = estado
        self.codigo = codigo
        self.mensaje = mensaje
