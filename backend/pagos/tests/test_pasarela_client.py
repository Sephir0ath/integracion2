"""Tests of the resilient client of the gateway.

They cover the four protections of the fragile dependency: the timeout, the
Bulkhead, the Circuit Breaker and the retry with exponential backoff.

The network is replaced by `FakeTransport`, so no pasarela is running.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from common.config import Settings
from common.errors import CODES, ServiceError
from common.metrics import REGISTRY

from ..pasarela import DEGRADADO_BULKHEAD, DEGRADADO_PASARELA, PasarelaClient
from .fakes import FakeTransport

RESPUESTA_OK = {"pago_id": "pgw_1", "estado": "CONFIRMADO", "referencia": "REF-1"}


def _settings(**cambios) -> Settings:
    base = dict(
        pasarela_url="http://pasarela.test",
        pasarela_timeout=2.0,
        pasarela_max_retries=2,
        circuit_failure_threshold=3,
        circuit_reset_timeout=0.05,
        circuit_half_open_max_calls=1,
        bulkhead_max_concurrent=5,
        bulkhead_max_pending=10,
    )
    base.update(cambios)
    return Settings(**base)


def _cliente(handler, **cambios):
    settings = _settings(**cambios)
    transporte = FakeTransport(handler)
    http = httpx.AsyncClient(transport=transporte)
    cliente = PasarelaClient(settings, client=http, retry_base_delay=0.01)
    return cliente, transporte


async def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=RESPUESTA_OK)


async def _caida(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no hay red", request=_request)


async def _error_503(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, json={"detail": "pasarela no disponible"})


# --------------------------------------------------------------- timeout --
async def test_el_cliente_usa_el_timeout_configurado():
    cliente = PasarelaClient(_settings(pasarela_timeout=1.5))

    assert cliente.http.timeout.connect == 1.5
    assert cliente.http.timeout.read == 1.5

    await cliente.close()


async def test_un_timeout_de_la_pasarela_es_un_fallo_transitorio():
    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("tarde", request=request)

    cliente, transporte = _cliente(timeout, pasarela_max_retries=0)

    with pytest.raises(ServiceError) as error:
        await cliente.cobrar(1000, "saga-1:COBRAR")

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
    assert len(transporte.llamadas) == 1


# ------------------------------------------------------- circuit breaker --
async def test_el_cobro_exitoso_devuelve_la_respuesta_de_la_pasarela():
    cliente, transporte = _cliente(_ok)

    respuesta = await cliente.cobrar(25000, "saga-1:COBRAR")

    assert respuesta == RESPUESTA_OK
    assert len(transporte.llamadas) == 1
    assert transporte.llamadas[0].url.path == "/pagos"


async def test_el_circuito_abre_tras_el_umbral_de_fallos_consecutivos():
    cliente, transporte = _cliente(_caida, circuit_failure_threshold=3, pasarela_max_retries=0)

    for _ in range(2):
        with pytest.raises(ServiceError):
            await cliente.cobrar(1000, "saga-1:COBRAR")
        assert cliente.breaker.state == "CLOSED"

    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-1:COBRAR")

    assert cliente.breaker.state == "OPEN"
    assert cliente.breaker.snapshot()["fallos_consecutivos"] == 3
    # Three failed calls, one attempt each: the retry does not multiply them.
    assert len(transporte.llamadas) == 3


async def test_con_el_circuito_abierto_la_llamada_no_llega_a_la_pasarela():
    cliente, transporte = _cliente(_caida, circuit_failure_threshold=1, pasarela_max_retries=0)

    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-1:COBRAR")
    assert cliente.breaker.state == "OPEN"

    llamadas = len(transporte.llamadas)

    with pytest.raises(ServiceError) as error:
        await cliente.cobrar(1000, "saga-2:COBRAR")

    assert error.value.code == CODES.CIRCUIT_OPEN
    assert len(transporte.llamadas) == llamadas, "el circuito abierto no puede tocar la red"
    assert cliente.breaker.snapshot()["llamadas_rechazadas"] == 1


async def test_el_circuito_pasa_a_half_open_y_cierra_con_una_sonda_exitosa():
    cliente, transporte = _cliente(_caida, circuit_failure_threshold=1, circuit_reset_timeout=0.05, pasarela_max_retries=0)

    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-1:COBRAR")
    assert cliente.breaker.state == "OPEN"

    await asyncio.sleep(0.06)

    # The probe reaches the gateway again and, when it answers, the circuit closes.
    transporte.handler = _ok
    respuesta = await cliente.cobrar(1000, "saga-2:COBRAR")

    assert respuesta == RESPUESTA_OK
    assert cliente.breaker.state == "CLOSED"
    assert len(transporte.llamadas) == 2


async def test_el_circuito_se_abre_de_nuevo_si_la_sonda_falla():
    cliente, _ = _cliente(_caida, circuit_failure_threshold=1, circuit_reset_timeout=0.05, pasarela_max_retries=0)

    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-1:COBRAR")
    await asyncio.sleep(0.06)
    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-2:COBRAR")

    assert cliente.breaker.state == "OPEN"


# -------------------------------------------------------------- bulkhead --
async def test_el_bulkhead_rechaza_el_exceso_de_inmediato():
    release = asyncio.Event()

    async def lenta(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=RESPUESTA_OK)

    cliente, transporte = _cliente(lenta, bulkhead_max_concurrent=1, bulkhead_max_pending=0)

    tarea = asyncio.create_task(cliente.cobrar(1000, "saga-1:COBRAR"))
    await asyncio.sleep(0.02)

    with pytest.raises(ServiceError) as error:
        await cliente.cobrar(1000, "saga-2:COBRAR")

    assert error.value.code == CODES.BULKHEAD_SATURATED
    assert len(transporte.llamadas) == 1, "la llamada rechazada no puede llegar a la red"

    release.set()
    assert await tarea == RESPUESTA_OK


async def test_el_rechazo_del_bulkhead_no_abre_el_circuito():
    release = asyncio.Event()

    async def lenta(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=RESPUESTA_OK)

    cliente, _ = _cliente(lenta, bulkhead_max_concurrent=1, bulkhead_max_pending=0)

    tarea = asyncio.create_task(cliente.cobrar(1000, "saga-1:COBRAR"))
    await asyncio.sleep(0.02)

    for _ in range(3):
        with pytest.raises(ServiceError) as error:
            await cliente.cobrar(1000, "saga-2:COBRAR")
        assert error.value.code == CODES.BULKHEAD_SATURATED

    release.set()
    await tarea

    assert cliente.breaker.state == "CLOSED", "el bulkhead no dice nada de la salud de la pasarela"


# ----------------------------------------------------------------- retry --
async def test_reintenta_ante_un_fallo_transitorio():
    intentos = {"n": 0}

    async def intermitente(request: httpx.Request) -> httpx.Response:
        intentos["n"] += 1
        if intentos["n"] == 1:
            return httpx.Response(503, json={"detail": "caida"})
        return httpx.Response(200, json=RESPUESTA_OK)

    cliente, transporte = _cliente(intermitente, pasarela_max_retries=2)

    respuesta = await cliente.cobrar(1000, "saga-1:COBRAR")

    assert respuesta == RESPUESTA_OK
    assert len(transporte.llamadas) == 2, "el fallo transitorio se reintenta"


async def test_reintenta_hasta_el_maximo_de_intentos():
    cliente, transporte = _cliente(_error_503, pasarela_max_retries=2)

    with pytest.raises(ServiceError) as error:
        await cliente.cobrar(1000, "saga-1:COBRAR")

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
    assert len(transporte.llamadas) == 3, "un intento mas los dos reintentos"


async def test_no_reintenta_mientras_el_circuito_esta_abierto():
    cliente, transporte = _cliente(_caida, circuit_failure_threshold=1, pasarela_max_retries=2)

    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-1:COBRAR")
    assert cliente.breaker.state == "OPEN"
    llamadas = len(transporte.llamadas)

    with pytest.raises(ServiceError) as error:
        await cliente.cobrar(1000, "saga-2:COBRAR")

    assert error.value.code == CODES.CIRCUIT_OPEN
    assert len(transporte.llamadas) == llamadas, "un circuito abierto no reintenta"


# --------------------------------------------------------- observabilidad --
async def test_el_estado_del_circuito_se_puede_consultar():
    cliente, _ = _cliente(_ok)

    estado = cliente.snapshot()

    assert estado["estado"] == "CLOSED"
    assert estado["fallos_consecutivos"] == 0
    assert estado["llamadas_rechazadas"] == 0
    assert estado["llamadas_en_curso"] == 0
    assert estado["rechazos_bulkhead"] == 0
    assert estado["fallback"] == ""


async def test_la_degradacion_se_reporta_mientras_el_circuito_esta_abierto():
    cliente, _ = _cliente(_caida, circuit_failure_threshold=1, pasarela_max_retries=0)

    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-1:COBRAR")

    assert cliente.snapshot()["fallback"] == DEGRADADO_PASARELA


async def test_la_degradacion_del_bulkhead_se_reporta():
    release = asyncio.Event()

    async def lenta(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(200, json=RESPUESTA_OK)

    cliente, _ = _cliente(lenta, bulkhead_max_concurrent=1, bulkhead_max_pending=0)

    tarea = asyncio.create_task(cliente.cobrar(1000, "saga-1:COBRAR"))
    await asyncio.sleep(0.02)
    with pytest.raises(ServiceError):
        await cliente.cobrar(1000, "saga-2:COBRAR")

    assert cliente.snapshot()["fallback"] == DEGRADADO_BULKHEAD

    release.set()
    await tarea


async def test_las_metricas_registran_las_llamadas_a_la_pasarela():
    antes_ok = REGISTRY.get_sample_value("pasarela_requests_total", {"result": "ok"}) or 0.0
    antes_error = REGISTRY.get_sample_value("pasarela_requests_total", {"result": "error"}) or 0.0

    cliente, _ = _cliente(_ok)
    await cliente.cobrar(1000, "saga-1:COBRAR")

    cliente_caida, _ = _cliente(_error_503, pasarela_max_retries=0)
    with pytest.raises(ServiceError):
        await cliente_caida.cobrar(1000, "saga-2:COBRAR")

    despues_ok = REGISTRY.get_sample_value("pasarela_requests_total", {"result": "ok"}) or 0.0
    despues_error = REGISTRY.get_sample_value("pasarela_requests_total", {"result": "error"}) or 0.0

    assert despues_ok == antes_ok + 1
    assert despues_error == antes_error + 1
