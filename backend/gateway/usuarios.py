"""Usuarios del gateway y su almacen.

Los usuarios salen de la variable de entorno `GATEWAY_USERS`: un JSON con
la lista

    [{"email": ..., "password": ..., "cliente_id": ..., "role": "admin"|"cliente"}]

Si `GATEWAY_USERS` no esta definida (o no es un JSON valido) se usa el
**juego de demostracion** de este modulo, para que el sistema levante sin
configurar nada:

| email                     | password     | cliente_id | role     |
|---------------------------|--------------|------------|----------|
| `admin@ticketera.local`   | `admin123`   | `admin-1`  | `admin`  |
| `cliente@ticketera.local` | `cliente123` | `cliente-1`| `cliente`|

`GATEWAY_USERS` no esta en `common.config.Settings` (la biblioteca comun es
de solo lectura), asi que se lee del entorno aqui.

Las contrasenas se guardan en claro porque el contrato del sistema las
define asi; la comparacion es de tiempo constante para no filtrar
informacion por el tiempo de respuesta.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass

from common.logging import log_event

logger = logging.getLogger("gateway.usuarios")

# Roles que el gateway entiende.
ROLES = ("admin", "cliente")


@dataclass(frozen=True)
class Usuario:
    """Usuario autorizado a entrar al sistema."""

    email: str
    password: str
    cliente_id: str
    role: str


# Juego de demostracion: un administrador y un cliente.
USUARIOS_DEMOSTRACION: tuple[Usuario, ...] = (
    Usuario(
        email="admin@ticketera.local",
        password="admin123",
        cliente_id="admin-1",
        role="admin",
    ),
    Usuario(
        email="cliente@ticketera.local",
        password="cliente123",
        cliente_id="cliente-1",
        role="cliente",
    ),
)


def usuarios_desde_entorno(valor: str | None = None) -> tuple[Usuario, ...]:
    """Usuarios de `GATEWAY_USERS`, o el juego de demostracion.

    Una entrada invalida (sin correo, sin clave, con un rol desconocido)
    se descarta con un aviso: es mejor entrar con el juego de
    demostracion que con una lista a medias.
    """
    if valor is None:
        valor = os.getenv("GATEWAY_USERS")
    if not valor:
        return USUARIOS_DEMOSTRACION

    try:
        contenido = json.loads(valor)
    except (TypeError, ValueError) as error:
        log_event(logger, "gateway_usuarios_invalidos", error=str(error))
        return USUARIOS_DEMOSTRACION

    if not isinstance(contenido, list):
        log_event(logger, "gateway_usuarios_invalidos", motivo="GATEWAY_USERS no es una lista")
        return USUARIOS_DEMOSTRACION

    usuarios: list[Usuario] = []
    for entrada in contenido:
        if not isinstance(entrada, dict):
            continue
        email = str(entrada.get("email") or "").strip()
        password = str(entrada.get("password") or "")
        cliente_id = str(entrada.get("cliente_id") or "").strip()
        role = str(entrada.get("role") or "").strip().lower()
        if not email or not password or not cliente_id or role not in ROLES:
            log_event(logger, "gateway_usuario_descartado", email=email, rol=role)
            continue
        usuarios.append(Usuario(email=email, password=password, cliente_id=cliente_id, role=role))

    if not usuarios:
        return USUARIOS_DEMOSTRACION
    return tuple(usuarios)


class AlmacenUsuarios:
    """Los usuarios que pueden entrar al sistema."""

    def __init__(self, usuarios=USUARIOS_DEMOSTRACION) -> None:
        self._usuarios: dict[str, Usuario] = {}
        for usuario in usuarios:
            self._usuarios[usuario.email.strip().lower()] = usuario

    def __len__(self) -> int:
        """Cantidad de usuarios registrados."""
        return len(self._usuarios)

    def autenticar(self, email: str, password: str) -> Usuario | None:
        """El usuario con esas credenciales, o `None`."""
        usuario = self._usuarios.get((email or "").strip().lower())
        if usuario is None:
            return None
        if not secrets.compare_digest(
            usuario.password.encode("utf-8"),
            (password or "").encode("utf-8"),
        ):
            return None
        return usuario
