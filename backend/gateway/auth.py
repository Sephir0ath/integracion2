"""Autenticacion del gateway: el token y la identidad del cliente.

El gateway valida el token **una sola vez**, en la entrada. A partir de ahi
los servicios internos confian en la identidad que llega en las cabeceras
`X-Client-Id` y `X-Role`: nadie mas vuelve a mirar el token.

El token se firma con PyJWT (`JWT_ALGORITHM`, HS256 por defecto) y lleva
estos claims:

* `sub`: el `cliente_id` del usuario;
* `cliente_id`: el `cliente_id` del usuario;
* `role`: `admin` o `cliente`;
* `exp` e `iat`: la vida del token (`JWT_TTL_SECONDS`).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import jwt

from common.config import Settings

from .errores import CODIGO_NO_AUTENTICADO, ErrorGateway
from .usuarios import Usuario

logger = logging.getLogger("gateway.auth")


@dataclass(frozen=True)
class Principal:
    """La identidad del cliente que abrio la peticion."""

    cliente_id: str
    role: str
    email: str = ""

    @property
    def es_admin(self) -> bool:
        """Si el cliente puede entrar a las rutas de administracion."""
        return self.role == "admin"

    def metadatos(self) -> tuple[tuple[str, str], ...]:
        """Las cabeceras de identidad que viajan al servicio interno.

        En gRPC las claves de las metadatos van en minusculas: por eso
        son `x-client-id` y `x-role` y no `X-Client-Id`.
        """
        return (("x-client-id", self.cliente_id), ("x-role", self.role))


def emitir_token(usuario: Usuario, settings: Settings) -> tuple[str, int]:
    """Emite el token del usuario.

    Devuelve el token y su vida en segundos.
    """
    ahora = int(time.time())
    claims = {
        "sub": usuario.cliente_id,
        "cliente_id": usuario.cliente_id,
        "role": usuario.role,
        "email": usuario.email,
        "iat": ahora,
        "exp": ahora + int(settings.jwt_ttl_seconds),
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(settings.jwt_ttl_seconds)


def extraer_token(cabecera: str | None) -> str | None:
    """El token de la cabecera `Authorization`, si lo hay."""
    if not cabecera:
        return None
    partes = cabecera.split()
    if len(partes) != 2 or partes[0].lower() != "bearer":
        return None
    return partes[1] or None


def verificar_token(token: str, settings: Settings) -> Principal:
    """Valida el token y devuelve la identidad del cliente.

    Un token invalido o vencido es un `ErrorGateway` de 401: la puerta no
    se abre y el fallo queda contado en `gateway_auth_failures_total`.
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as error:
        raise ErrorGateway(401, CODIGO_NO_AUTENTICADO, "el token ya expiro") from error
    except jwt.InvalidTokenError as error:
        raise ErrorGateway(401, CODIGO_NO_AUTENTICADO, "el token no es valido") from error

    cliente_id = str(claims.get("cliente_id") or claims.get("sub") or "")
    if not cliente_id:
        raise ErrorGateway(401, CODIGO_NO_AUTENTICADO, "el token no identifica a ningun cliente")
    return Principal(
        cliente_id=cliente_id,
        role=str(claims.get("role") or "cliente"),
        email=str(claims.get("email") or ""),
    )
