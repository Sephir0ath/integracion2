"""Pruebas del limite de peticiones por cliente.

El limitador es un **tazon de fichas** (token bucket) por cliente: la
capacidad es el burst y la recarga es el limite por minuto. El estado vive
en Redis para que valga entre réplicas del gateway, y cuando Redis no
responde el gateway sigue sirviendo y sigue limitando con cubos en
memoria: la caida de Redis no puede tumbar el sistema.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from .. import app as modulo_app
from ..app import crear_app
from ..rate_limit import LimitadorTazon, consumir_tokens
from .conftest import cliente_para, valor_contador
from .fakes import FakeRedis


# ------------------------------------------------------- la semantica --


def test_el_tazon_no_deja_pasar_mas_que_la_capacidad():
    """Con el tazon lleno solo pasan `capacidad` peticiones."""
    ahora = 1000.0
    tokens = 20.0

    for _ in range(20):
        permitido, tokens, _ = consumir_tokens(tokens, ahora, ahora, 20.0, 1.0)
        assert permitido is True

    permitido, tokens, _ = consumir_tokens(tokens, ahora, ahora, 20.0, 1.0)
    assert permitido is False
    assert tokens == 0.0


def test_el_tazon_recarga_con_el_tiempo():
    """El cubo se recarga solo, sin pasar de la capacidad."""
    ahora = 1000.0

    # Un segundo despues hay una ficha (la recarga es de 1 por segundo).
    permitido, fichas, _ = consumir_tokens(0.0, ahora, ahora + 1.0, 20.0, 1.0)
    assert permitido is True
    assert fichas == 0.0

    # Y el tazon nunca guarda mas de la capacidad (costo 0: no se consume).
    _, fichas, _ = consumir_tokens(20.0, ahora, ahora + 3600.0, 20.0, 1.0, costo=0.0)
    assert fichas == 20.0


# ------------------------------------------------------ el limite en la puerta --


async def test_el_limite_rechaza_al_pasar_el_burst(construir_app, tokens):
    """Al pasar el burst la peticion se rechaza con 429 y `Retry-After`."""
    limitador = LimitadorTazon(redis=None, capacidad=2, recarga_por_minuto=60)
    app = construir_app(limitador=limitador)
    antes = valor_contador("gateway_rate_limit_rejected_total")
    cabecera = {"Authorization": f"Bearer {tokens.cliente()}"}

    async with cliente_para(app) as http:
        assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200
        assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200

        respuesta = await http.get("/api/v1/butacas/eventos", headers=cabecera)

    assert respuesta.status_code == 429
    assert respuesta.headers["Retry-After"]
    assert int(respuesta.headers["Retry-After"]) >= 1
    assert valor_contador("gateway_rate_limit_rejected_total") == antes + 1


async def test_el_limite_es_por_cliente(construir_app, tokens):
    """Cada cliente tiene su propio tazon."""
    limitador = LimitadorTazon(redis=None, capacidad=1, recarga_por_minuto=60)
    app = construir_app(limitador=limitador)
    admin = {"Authorization": f"Bearer {tokens.admin()}"}
    cliente = {"Authorization": f"Bearer {tokens.cliente()}"}

    async with cliente_para(app) as http:
        assert (await http.get("/api/v1/butacas/eventos", headers=admin)).status_code == 200
        # El tazon del admin esta lleno: el cliente entra igual.
        assert (await http.get("/api/v1/butacas/eventos", headers=cliente)).status_code == 200
        assert (await http.get("/api/v1/butacas/eventos", headers=admin)).status_code == 429
        assert (await http.get("/api/v1/butacas/eventos", headers=cliente)).status_code == 429


async def test_con_redis_caido_el_gateway_sigue_respondiendo(construir_app, tokens, redis_caido):
    """Redis caido no tumba el gateway: sigue sirviendo y sigue limitando."""
    limitador = LimitadorTazon(redis=redis_caido, capacidad=2, recarga_por_minuto=60)
    app = construir_app(limitador=limitador)
    cabecera = {"Authorization": f"Bearer {tokens.cliente()}"}

    async with cliente_para(app) as http:
        # Las rutas publicas siguen respondiendo.
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200
        assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200
        # Y el limite sigue aplicando, con el cubo en memoria.
        respuesta = await http.get("/api/v1/butacas/eventos", headers=cabecera)

    assert respuesta.status_code == 429
    assert respuesta.headers["Retry-After"]


async def test_con_redis_sano_el_limite_usa_el_cubo_compartido(construir_app, tokens):
    """Con Redis sano el cubo vive en Redis, no en el proceso."""
    redis = FakeRedis()
    limitador = LimitadorTazon(redis=redis, capacidad=2, recarga_por_minuto=60)
    app = construir_app(limitador=limitador)
    cabecera = {"Authorization": f"Bearer {tokens.cliente()}"}

    async with cliente_para(app) as http:
        for _ in range(2):
            assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200
        respuesta = await http.get("/api/v1/butacas/eventos", headers=cabecera)

    assert respuesta.status_code == 429
    assert redis.evaluaciones == 3


async def test_la_caida_de_redis_no_se_confunde_con_un_rechazo(construir_app, tokens, redis_caido):
    """Un Redis caido no puede parecer un limite alcanzado.

    El contador de rechazos solo sube cuando hay un rechazo: una
    peticion que pasa lo deja igual, y una que se rechaza lo sube en
    exactamente uno. La asercion anterior (`antes >= 0`) era tautologica:
    no comprobaba nada.
    """
    # La recarga es de una ficha por minuto: veinte peticiones seguidas
    # no alcanzan a llenar ni una ficha, asi que el veredicto no depende
    # de la velocidad de la maquina.
    limitador = LimitadorTazon(redis=redis_caido, capacidad=20, recarga_por_minuto=1)
    app = construir_app(limitador=limitador)
    cabecera = {"Authorization": f"Bearer {tokens.cliente()}"}

    async with cliente_para(app) as http:
        # Las peticiones que pasan no cuentan como rechazo.
        for _ in range(20):
            antes = valor_contador("gateway_rate_limit_rejected_total")
            respuesta = await http.get("/api/v1/butacas/eventos", headers=cabecera)
            assert respuesta.status_code == 200
            assert valor_contador("gateway_rate_limit_rejected_total") == antes

        # Y la que se rechaza cuenta exactamente un rechazo.
        antes = valor_contador("gateway_rate_limit_rejected_total")
        respuesta = await http.get("/api/v1/butacas/eventos", headers=cabecera)

    assert respuesta.status_code == 429
    assert valor_contador("gateway_rate_limit_rejected_total") == antes + 1


# ------------------------------- el cableado de la configuracion al limite --
#
# En las pruebas la app siempre recibe el limitador ya hecho: por eso
# `RATE_LIMIT_BURST` y `RATE_LIMIT_PER_MINUTE` no llegaban nunca al
# limitador y nadie se enteraba de que el limite real era otro.


async def test_la_app_construye_el_limitador_con_la_configuracion(
    monkeypatch, config, almacen, clientes, cliente_bff
):
    """La app pasa el burst y el limite por minuto de la configuracion al limitador."""
    # Burst y limite por minuto distintos de los de por defecto (20 y 60),
    # para que no se puedan confundir con los del constructor.
    settings = replace(config.settings, rate_limit_burst=7, rate_limit_per_minute=90)
    config = replace(config, settings=settings)

    construidos: list[dict] = []

    class LimitadorEspia(LimitadorTazon):
        """El limitador de verdad, que ademas deja registro de su construccion."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            construidos.append({"args": args, "kwargs": kwargs, "limitador": self})

    monkeypatch.setattr(modulo_app, "LimitadorTazon", LimitadorEspia)

    crear_app(
        config,
        almacen=almacen,
        butacas=clientes["butacas"],
        compras=clientes["compras"],
        emision=clientes["emision"],
        cliente_bff=cliente_bff,
    )

    assert len(construidos) == 1
    construccion = construidos[0]
    # El cableado: la capacidad es el burst y la recarga es el limite por minuto.
    assert construccion["kwargs"]["capacidad"] == 7
    assert construccion["kwargs"]["recarga_por_minuto"] == 90
    # Y la conversion de por minuto a por segundo que hace el limitador.
    limitador = construccion["limitador"]
    assert limitador.capacidad == 7
    assert limitador.recarga_por_segundo == pytest.approx(90 / 60)


async def test_el_limite_que_se_aplica_es_el_de_la_configuracion(
    config, almacen, clientes, cliente_bff, tokens
):
    """Con la configuracion de la app, el burst es el limite y el rate la recarga.

    La prueba es de efecto, no de estructura: se agota el tazon con el
    burst de la configuracion y el `Retry-After` dice cuantos segundos
    tarda la recarga, que es el limite por minuto convertido a segundos.
    """
    settings = replace(config.settings, rate_limit_burst=2, rate_limit_per_minute=1)
    config = replace(config, settings=settings)
    app = crear_app(
        config,
        almacen=almacen,
        butacas=clientes["butacas"],
        compras=clientes["compras"],
        emision=clientes["emision"],
        cliente_bff=cliente_bff,
    )
    cabecera = {"Authorization": f"Bearer {tokens.cliente()}"}

    async with cliente_para(app) as http:
        assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200
        assert (await http.get("/api/v1/butacas/eventos", headers=cabecera)).status_code == 200
        respuesta = await http.get("/api/v1/butacas/eventos", headers=cabecera)

    # El burst de la configuracion son dos peticiones: la tercera se rechaza.
    assert respuesta.status_code == 429
    # Y la recarga es de una ficha por minuto: hay que esperar sesenta
    # segundos (con el valor de por defecto, de un minuto, serian uno).
    assert int(respuesta.headers["Retry-After"]) == 60
