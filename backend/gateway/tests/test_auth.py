"""Pruebas de la autenticacion del gateway.

El gateway es la unica puerta del sistema: valida el token **una sola vez**
y reenvia la identidad del cliente a los servicios internos, que confian
en ella y no vuelven a validar el token.
"""

from __future__ import annotations

import time

import jwt
import pytest

from .conftest import valor_contador

# Las rutas de administracion del recinto, con el RPC de `butacas` que
# cada una debe terminar llamando y con que peticion.
RUTAS_DE_ADMIN = [
    (
        "POST",
        "/api/v1/butacas/eventos",
        {"nombre": "n", "lugar": "l", "fecha": "2026-05-01T20:00:00"},
        "CrearEvento",
        {"nombre": "n", "lugar": "l", "fecha": "2026-05-01T20:00:00"},
    ),
    (
        "PUT",
        "/api/v1/butacas/eventos/ev-1",
        {"nombre": "n", "lugar": "l", "fecha": "2026-05-01T20:00:00"},
        "ActualizarEvento",
        {"evento_id": "ev-1", "nombre": "n", "lugar": "l", "fecha": "2026-05-01T20:00:00"},
    ),
    (
        "DELETE",
        "/api/v1/butacas/eventos/ev-1",
        None,
        "EliminarEvento",
        {"evento_id": "ev-1"},
    ),
    (
        "POST",
        "/api/v1/butacas/secciones",
        {"evento_id": "ev-1", "nombre": "s", "capacidad_total": 10, "precio_centavos": 1000},
        "CrearSeccion",
        {"evento_id": "ev-1", "nombre": "s", "capacidad_total": 10, "precio_centavos": 1000},
    ),
    (
        "PUT",
        "/api/v1/butacas/secciones/sec-1",
        {"nombre": "s", "capacidad_total": 10, "precio_centavos": 1000},
        "ActualizarSeccion",
        {"seccion_id": "sec-1", "nombre": "s", "capacidad_total": 10, "precio_centavos": 1000},
    ),
    (
        "DELETE",
        "/api/v1/butacas/secciones/sec-1",
        None,
        "EliminarSeccion",
        {"seccion_id": "sec-1"},
    ),
]

# Los identificadores de cada caso, para que un fallo diga que ruta se cayo.
IDS_DE_ADMIN = [caso[3] for caso in RUTAS_DE_ADMIN]

# ------------------------------------------------------- el token publico --


async def test_token_devuelve_el_contrato_del_endpoint(http):
    """El endpoint de token responde el contrato exacto del borde."""
    respuesta = await http.post(
        "/api/v1/auth/token",
        json={"email": "admin@ticketera.local", "password": "admin123"},
    )

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["token_type"] == "bearer"
    assert cuerpo["expires_in"] == 3600
    assert cuerpo["cliente_id"] == "admin-1"
    assert cuerpo["role"] == "admin"
    assert isinstance(cuerpo["access_token"], str)
    assert cuerpo["access_token"]


async def test_token_con_password_incorrecto_es_401(http):
    """Con la contrasena equivocada no hay token."""
    respuesta = await http.post(
        "/api/v1/auth/token",
        json={"email": "admin@ticketera.local", "password": "no-es-la-clave"},
    )

    assert respuesta.status_code == 401
    assert "access_token" not in respuesta.json()


async def test_token_con_usuario_desconocido_es_401(http):
    """Un usuario que no existe no recibe token."""
    respuesta = await http.post(
        "/api/v1/auth/token",
        json={"email": "nadie@ticketera.local", "password": "admin123"},
    )

    assert respuesta.status_code == 401


# --------------------------------------------------- el token obligatorio --


async def test_ruta_de_negocio_sin_token_es_401(http):
    """Sin token no se entra, y el fallo de autenticacion se cuenta."""
    antes = valor_contador("gateway_auth_failures_total")

    respuesta = await http.get("/api/v1/butacas/eventos")

    assert respuesta.status_code == 401
    assert valor_contador("gateway_auth_failures_total") == antes + 1


async def test_ruta_de_negocio_con_token_invalido_es_401(http):
    """Un token que nadie firmo no abre la puerta."""
    antes = valor_contador("gateway_auth_failures_total")

    respuesta = await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": "Bearer esto-no-es-un-jwt"},
    )

    assert respuesta.status_code == 401
    assert valor_contador("gateway_auth_failures_total") == antes + 1


async def test_ruta_de_negocio_con_token_vencido_es_401(http, settings):
    """Un token vencido se rechaza como cualquier token invalido.

    El rechazo por token vencido tambien es un fallo de autenticacion, asi
    que tiene que quedar contado en `gateway_auth_failures_total`.
    """
    ahora = int(time.time())
    vencido = jwt.encode(
        {
            "sub": "cliente-1",
            "cliente_id": "cliente-1",
            "role": "cliente",
            "iat": ahora - 7200,
            "exp": ahora - 3600,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    antes = valor_contador("gateway_auth_failures_total")

    respuesta = await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": f"Bearer {vencido}"},
    )

    assert respuesta.status_code == 401
    assert valor_contador("gateway_auth_failures_total") == antes + 1


async def test_ruta_de_negocio_con_token_valido_pasa(http, stubs, tokens):
    """Con token valido la peticion llega al servicio interno."""
    respuesta = await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {
        "eventos": [
            {
                "evento_id": "ev-1",
                "nombre": "Partido decisivo",
                "lugar": "El Campin",
                "fecha": "2026-03-01T20:00:00",
            },
            {
                "evento_id": "ev-2",
                "nombre": "Concierto sinfonico",
                "lugar": "Coliseo",
                "fecha": "2026-04-01T20:00:00",
            },
        ]
    }
    # El servicio interno recibio la identidad del cliente.
    metadatos = stubs["butacas"].ultima_llamada["metadata"]
    assert metadatos["x-client-id"] == "cliente-1"
    assert metadatos["x-role"] == "cliente"


async def test_el_token_se_valida_una_sola_vez(http, stubs, tokens):
    """La identidad viaja como metadatos, no como un token que el interno revalide."""
    respuesta = await http.get(
        "/api/v1/butacas/eventos",
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    metadatos = stubs["butacas"].ultima_llamada["metadata"]
    assert metadatos["x-client-id"] == "admin-1"
    assert metadatos["x-role"] == "admin"
    assert "authorization" not in metadatos


# ---------------------------------------------------------- el rol admin --


@pytest.mark.parametrize(
    "metodo,ruta,cuerpo,rpc,peticion_esperada",
    RUTAS_DE_ADMIN,
    ids=IDS_DE_ADMIN,
)
async def test_ruta_de_admin_con_rol_cliente_es_403(http, stubs, tokens, metodo, ruta, cuerpo, rpc, peticion_esperada):
    """Las rutas de administracion exigen el rol `admin`.

    El 403 tiene que ser del **gateway**: el servicio interno no se
    invoca ni una vez, y el cuerpo lleva el codigo de error del borde.
    """
    respuesta = await http.request(
        metodo,
        ruta,
        json=cuerpo,
        headers={"Authorization": f"Bearer {tokens.cliente()}"},
    )

    assert respuesta.status_code == 403
    # El codigo del borde, no uno de los de `common.errors`.
    assert respuesta.json()["code"] == "SIN_PERMISO"
    # Y el servicio interno no se entero de la peticion.
    assert stubs["butacas"].calls == []


@pytest.mark.parametrize(
    "metodo,ruta,cuerpo,rpc,peticion_esperada",
    RUTAS_DE_ADMIN,
    ids=IDS_DE_ADMIN,
)
async def test_ruta_de_admin_con_rol_admin_pasa(http, stubs, tokens, metodo, ruta, cuerpo, rpc, peticion_esperada):
    """Con el rol `admin` la ruta de administracion llega al servicio interno.

    No basta con que el stub haya recibido *algo*: la llamada tiene que
    ser el RPC que corresponde a la ruta, con la peticion que la ruta
    pide y con la identidad del cliente.
    """
    respuesta = await http.request(
        metodo,
        ruta,
        json=cuerpo,
        headers={"Authorization": f"Bearer {tokens.admin()}"},
    )

    assert respuesta.status_code == 200
    llamadas = stubs["butacas"].calls
    assert len(llamadas) == 1
    llamada = llamadas[0]
    assert llamada["metodo"] == rpc
    peticion = llamada["request"]
    for campo, valor in peticion_esperada.items():
        assert getattr(peticion, campo) == valor
    # Y la identidad del cliente viaja al servicio interno.
    metadatos = llamada["metadata"]
    assert metadatos["x-client-id"] == "admin-1"
    assert metadatos["x-role"] == "admin"
