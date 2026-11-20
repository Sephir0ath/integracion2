"""Prueba de integracion del limitador contra un Redis real.

El doble de Redis de `tests/fakes.py` **no ejecuta el script de Lua**:
llama a la funcion pura `consumir_tokens`, que es la referencia
documentada del script. Con ese doble, un error real dentro del script
(un `ARGV` mal leido, una recarga mal calculada, un `PEXPIRE` a la
carga equivocada) jamas lo veria la suite, y un cambio en el orden de los
`ARGV` se replicaria en el doble sin que nadie se entere: la suite
seguiria en verde.

Esta prueba cierra ese hueco: ejecuta **el script de Lua de verdad**
contra **un Redis de verdad** y lo compara, caso por caso, con la funcion
de referencia.

La prueba se salta cuando no hay un Redis alcanzable. Se usa la variable
de entorno `REDIS_URL`: si no esta definida la suite normal corre igual
(verde, con esta prueba saltada) y la de integracion se corre dentro de
la red de compose:

    REDIS_URL=redis://redis:6379/0 python -m pytest -q
"""

from __future__ import annotations

import os
import uuid

import pytest

from ..rate_limit import (
    ESCRIPT_TAZON,
    PREFIJO_CLAVE,
    LimitadorTazon,
    consumir_tokens,
)

# El script escribe su marca de tiempo con `tostring`, que en Lua deja 14
# cifras significativas: un instante de la epoca se guarda con cuatro
# decimales, asi que el reloj que se lee de vuelta puede diferir del que
# uso el script en hasta 5e-5 segundos. Ese margen, traducido a fichas
# con la recarga, es la tolerancia de la comparacion.


def margen_de(recarga: float) -> float:
    """La tolerancia de la comparacion, en fichas."""
    return 2e-4 * abs(recarga) + 1e-9


# ------------------------------------------------------------- el doble --


class RedisQueRegistra:
    """Cliente real de Redis que ademas deja registro de las llamadas.

    No cambia nada de lo que hace Redis: ejecuta el script de verdad y
    guarda los argumentos con los que fue llamado. Sirve para comprobar
    el **orden de los ARGV** en el punto de llamada, que es justo lo que
    el doble de `tests/fakes.py` no puede ver.
    """

    def __init__(self, interno) -> None:
        self._interno = interno
        self.evaluaciones: list[dict] = []

    async def eval(self, script, numkeys, *args):
        """Ejecuta el script en el Redis real y registra la llamada."""
        self.evaluaciones.append({"script": script, "numkeys": numkeys, "args": args})
        return await self._interno.eval(script, numkeys, *args)


# ------------------------------------------------------------- los utiles --


async def reloj_del_servidor(redis) -> float:
    """La hora del servidor de Redis, en segundos con decimales.

    El script usa el reloj del servidor (`redis.call('TIME')`), asi que
    la referencia tiene que compararse contra ese mismo reloj.
    """
    segundos, micros = await redis.time()
    return int(segundos) + int(micros) / 1_000_000


async def sembrar(redis, clave: str, fichas: float, ts: float) -> None:
    """Deja el cubo del cliente en un estado conocido.

    Es el estado que el propio script escribe: el hash de la clave con
    las fichas que quedan y la marca de tiempo de la ultima cuenta.
    """
    await redis.delete(clave)
    await redis.hset(clave, mapping={"tokens": str(fichas), "ts": str(ts)})


async def estado(redis, clave: str) -> tuple[float, float]:
    """Las fichas que quedan y la marca de tiempo que guardo el script."""
    guardado = await redis.hgetall(clave)
    return float(guardado["tokens"]), float(guardado["ts"])


# ------------------------------------------------------------- las claves --


@pytest.fixture()
async def redis_real():
    """Cliente de Redis real; si no hay uno alcanzable, la prueba se salta."""
    url = os.getenv("REDIS_URL")
    if not url:
        pytest.skip("no hay REDIS_URL: la prueba del script Lua real se salta")

    import redis.asyncio as redis_asyncio

    cliente = redis_asyncio.from_url(url, decode_responses=True)
    try:
        await cliente.ping()
    except Exception as error:  # noqa: BLE001 - sin Redis la prueba se salta
        await cliente.aclose()
        pytest.skip(f"no hay un Redis alcanzable en {url}: {error}")

    try:
        yield cliente
    finally:
        await cliente.aclose()


@pytest.fixture()
async def claves(redis_real):
    """Claves efimeras: cada prueba usa la suya y la borra al terminar."""

    class Claves:
        """Manejador de claves de la prueba."""

        def __init__(self) -> None:
            self._usadas: list[str] = []

        async def nueva(self, nombre: str = "prueba") -> str:
            """Una clave nueva y unica para la prueba."""
            clave = f"{PREFIJO_CLAVE}{nombre}-{uuid.uuid4().hex}"
            self._usadas.append(clave)
            return clave

        def usar(self, clave: str) -> str:
            """Registra una clave creada fuera para borrarla al terminar."""
            self._usadas.append(clave)
            return clave

        async def limpiar(self) -> None:
            """Borra las claves que dejo la prueba."""
            for clave in self._usadas:
                await redis_real.delete(clave)

    manejador = Claves()
    try:
        yield manejador
    finally:
        await manejador.limpiar()


# ------------------------------------------------------- el script real --


async def test_el_script_lua_real_coincide_con_la_funcion_de_referencia(redis_real, claves):
    """Con el tazon lleno el script real deja pasar la peticion.

    El script y la funcion `consumir_tokens` tienen que dar el mismo
    veredicto y las mismas fichas: esa es la relacion que el doble de
    `tests/fakes.py` da por Supuesta y por eso no puede validar al
    script de verdad.
    """
    clave = await claves.nueva("referencia")
    capacidad = 20.0
    recarga = 1.0
    costo = 1.0
    ahora = await reloj_del_servidor(redis_real)
    await sembrar(redis_real, clave, capacidad, ahora)

    respuesta = await redis_real.eval(
        ESCRIPT_TAZON, 1, clave, capacidad, recarga, costo, 60_000
    )
    fichas, ts = await estado(redis_real, clave)

    # La referencia se calcula con la marca de tiempo que escribio el
    # propio script: asi las dos cuentas usan exactamente el mismo reloj.
    esperado = consumir_tokens(capacidad, ahora, ts, capacidad, recarga, costo)
    margen = margen_de(recarga)
    assert esperado[0] is True
    assert int(respuesta[0]) == 1
    assert float(respuesta[1]) == pytest.approx(esperado[1], abs=margen)
    assert fichas == pytest.approx(esperado[1], abs=margen)


async def test_el_script_lua_real_rechaza_un_tazon_vacio(redis_real, claves):
    """Con el tazon vacio el script real rechaza la peticion.

    La recarga es cero a proposito: asi el veredicto no depende de cuanto
    tarde la prueba en llegar al script.
    """
    clave = await claves.nueva("vacio")
    capacidad = 20.0
    recarga = 0.0
    costo = 1.0
    ahora = await reloj_del_servidor(redis_real)
    await sembrar(redis_real, clave, 0.0, ahora)

    respuesta = await redis_real.eval(
        ESCRIPT_TAZON, 1, clave, capacidad, recarga, costo, 60_000
    )
    fichas, ts = await estado(redis_real, clave)

    esperado = consumir_tokens(0.0, ahora, ts, capacidad, recarga, costo)
    margen = margen_de(recarga)
    assert esperado[0] is False
    assert int(respuesta[0]) == 0
    assert float(respuesta[1]) == pytest.approx(esperado[1], abs=margen)
    # Un rechazo no consume ficha: el cubo sigue vacio.
    assert fichas == pytest.approx(0.0, abs=margen)


async def test_el_script_lua_real_recarga_por_el_tiempo_transcurrido(redis_real, claves):
    """Un cubo vacio se vuelve a llenar con el tiempo transcurrido.

    El cubo se sembro hace diez segundos con la recarga en medio fichaje
    por segundo: el script tiene que devolver unas cinco fichas menos
    una, no el cero del cubo vacio.
    """
    clave = await claves.nueva("recarga")
    capacidad = 20.0
    recarga = 0.5
    costo = 1.0
    ahora = await reloj_del_servidor(redis_real)
    await sembrar(redis_real, clave, 0.0, ahora - 10.0)

    respuesta = await redis_real.eval(
        ESCRIPT_TAZON, 1, clave, capacidad, recarga, costo, 60_000
    )
    fichas, ts = await estado(redis_real, clave)

    esperado = consumir_tokens(0.0, ahora - 10.0, ts, capacidad, recarga, costo)
    margen = margen_de(recarga)
    assert esperado[0] is True
    assert int(respuesta[0]) == 1
    assert float(respuesta[1]) == pytest.approx(esperado[1], abs=margen)
    assert fichas == pytest.approx(esperado[1], abs=margen)
    # Y el cubo se lleno con lo que da el tiempo, no con la capacidad
    # entera (veinte fichas).
    assert 4.0 < float(respuesta[1]) < 6.0


# ------------------------------------------------- el orden de los ARGV --
#
# El contrato del script es:
#
#   ARGV[1] capacidad   ARGV[2] recarga por segundo
#   ARGV[3] costo       ARGV[4] ttl en milisegundos
#
# Cada caso usa valores distintos en cada posicion, de modo que un orden
# distinto daria un resultado distinto (y por tanto el caso se cae).
CASOS_ARGV = [
    # (nombre, capacidad, recarga, costo, ttl, fichas, segundos, permitido, fichas finales)
    ("capacidad", 5.0, 1.0, 1.0, 90_000, 5.0, 100.0, 1, 4.0),
    ("recarga", 50.0, 2.0, 1.0, 90_000, 0.0, 10.0, 1, 19.0),
    ("costo", 50.0, 0.0, 3.0, 90_000, 10.0, 0.0, 1, 7.0),
    ("vacio", 5.0, 0.0, 1.0, 90_000, 0.0, 0.0, 0, 0.0),
]


@pytest.mark.parametrize(
    "nombre,capacidad,recarga,costo,ttl,fichas_iniciales,segundos,permitido,fichas_finales",
    CASOS_ARGV,
)
async def test_el_script_lua_real_recibe_los_argumentos_en_el_orden_del_contrato(
    redis_real,
    claves,
    nombre,
    capacidad,
    recarga,
    costo,
    ttl,
    fichas_iniciales,
    segundos,
    permitido,
    fichas_finales,
):
    """El script lee los ARGV en el orden del contrato: capacidad, recarga, costo y ttl."""
    clave = await claves.nueva(f"argv-{nombre}")
    ahora = await reloj_del_servidor(redis_real)
    await sembrar(redis_real, clave, fichas_iniciales, ahora - segundos)

    respuesta = await redis_real.eval(
        ESCRIPT_TAZON, 1, clave, capacidad, recarga, costo, ttl
    )
    fichas, ts = await estado(redis_real, clave)

    esperado = consumir_tokens(
        fichas_iniciales, ahora - segundos, ts, capacidad, recarga, costo
    )
    margen = margen_de(recarga)
    assert int(respuesta[0]) == permitido
    assert float(respuesta[1]) == pytest.approx(esperado[1], abs=margen)
    # El valor que da el orden del contrato (con margen para el reloj).
    assert float(respuesta[1]) == pytest.approx(fichas_finales, abs=2.0)
    # Y el ttl es el cuarto argumento: la clave expira con ese valor.
    assert 0 < await redis_real.pttl(clave) <= ttl


async def test_el_limitador_de_produccion_pasa_los_argumentos_al_script_en_orden(
    redis_real, claves
):
    """El limitador pasa capacidad, recarga por segundo, costo y ttl, en ese orden.

    Este es el otro lado del contrato: no solo que el script lea bien sus
    argumentos, sino que el limitador se los pase en el orden que el
    script espera. El doble de `tests/fakes.py` no puede detectarlo,
    porque lee los argumentos por posicion igual que el script.
    """
    registro = RedisQueRegistra(redis_real)
    limitador = LimitadorTazon(redis=registro, capacidad=5, recarga_por_minuto=120)
    cliente = f"argv-produccion-{uuid.uuid4().hex}"
    claves.usar(f"{PREFIJO_CLAVE}{cliente}")

    decision = await limitador.permitir(cliente)

    assert decision.permitido is True
    assert decision.almacen == "redis"
    assert len(registro.evaluaciones) == 1
    llamada = registro.evaluaciones[0]
    assert llamada["script"] == ESCRIPT_TAZON
    assert llamada["numkeys"] == 1
    nombre_clave, capacidad, recarga, costo, ttl = llamada["args"]
    assert nombre_clave == f"{PREFIJO_CLAVE}{cliente}"
    assert float(capacidad) == 5.0
    assert float(recarga) == 2.0  # 120 por minuto son 2 por segundo
    assert float(costo) == 1.0
    assert int(ttl) > 0
    # Y el cubo quedo en Redis con lo que conto el script de verdad.
    fichas, _ = await estado(redis_real, nombre_clave)
    assert fichas == pytest.approx(4.0, abs=0.05)


async def test_el_limitador_con_redis_real_rechaza_al_pasar_el_burst(redis_real, claves):
    """Con Redis real el cubo compartido se agota y la peticion se rechaza.

    La recarga es de una ficha por minuto: las tres peticiones van seguidas
    y el veredicto no depende de la velocidad de la maquina.
    """
    cliente = f"burst-{uuid.uuid4().hex}"
    claves.usar(f"{PREFIJO_CLAVE}{cliente}")
    limitador = LimitadorTazon(redis=redis_real, capacidad=2, recarga_por_minuto=1)

    decisiones = [await limitador.permitir(cliente) for _ in range(3)]

    assert [decision.permitido for decision in decisiones] == [True, True, False]
    assert decisiones[-1].almacen == "redis"
    assert decisiones[-1].reintento_tras >= 1
