"""Tests of the protection of the emission and of its observability.

They check the properties that are easy to lose in a refactor: the
database is not held while the dependency is consulted, a seat never
gets two tickets of the same saga, the metrics carry the result of every
operation and an injected fault makes the emission fail without leaving
a ticket behind.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from common.errors import CODES, ServiceError
from common.metrics import REGISTRY

from ..faults import FaultController, ModoInvalido
from ..models import Entrada
from ..service import EmisionService


def _entradas(sesiones) -> list[Entrada]:
    with sesiones() as session:
        return list(session.execute(select(Entrada)).scalars().all())


def _contador(nombre: str, resultado: str) -> float:
    return REGISTRY.get_sample_value(nombre, {"result": resultado}) or 0.0


async def test_la_base_de_datos_no_se_retiene_durante_la_llamada(
    servicio, butacas, conexiones_en_uso
):
    """La dependencia se consulta sin ninguna transaccion abierta.

    Si la transaccion quedara abierta, un `pagos` lento bloquearia al
    resto de las emisiones.
    """
    await servicio.emitir("saga-1", "pg-1", butacas)

    assert conexiones_en_uso, "la llamada a pagos no llego a hacerse"
    assert conexiones_en_uso == [0], "la base de datos estaba ocupada durante la llamada"


async def test_la_misma_butaca_no_recibe_dos_entradas(servicio, butacas, sesiones):
    """El mismo asiento no puede tener dos entradas de la misma saga."""
    await servicio.emitir("saga-1", "pg-1", butacas)

    resultado = await servicio.emitir("saga-1", "pg-1", butacas, idempotency_key="otra-clave")

    assert len(_entradas(sesiones)) == 2
    assert len({entrada.butaca_id for entrada in _entradas(sesiones)}) == 2
    assert resultado["idempotente"] is True


async def test_un_lote_a_medias_no_sobrevive(servicio, butacas, sesiones):
    """Un lote de entradas se escribe entero o no se escribe nada.

    La primera butaca del pedido es nueva y la segunda ya tiene entrada:
    el lote entero se revierte, asi que la butaca nueva tampoco queda.
    """
    await servicio.emitir("saga-1", "pg-1", butacas)

    pedido = [
        {"butaca_id": "sec-1-B9", "seccion_id": "sec-1", "fila": "B", "numero": 9},
        butacas[0],
    ]
    resultado = await servicio.emitir("saga-1", "pg-1", pedido, idempotency_key="lote-parcial")

    assert resultado["idempotente"] is True, "el choque es el del asiento repetido"
    butacas_de_la_saga = {entrada.butaca_id for entrada in _entradas(sesiones)}
    assert butacas_de_la_saga == {"sec-1-A1", "sec-1-A2"}
    assert "sec-1-B9" not in butacas_de_la_saga, "no puede quedar media entrada del lote"


async def test_las_metricas_cuentan_cada_resultado(servicio, butacas, pagos_stub):
    antes_ok = _contador("emision_entradas_total", "ok")
    antes_confirmado = _contador("emision_verificacion_pago_total", "confirmado")

    await servicio.emitir("saga-1", "pg-1", butacas)

    assert _contador("emision_entradas_total", "ok") == antes_ok + 1
    assert _contador("emision_verificacion_pago_total", "confirmado") == antes_confirmado + 1


async def test_las_metricas_cuentan_un_pago_no_confirmado(servicio, butacas, pagos_stub):
    antes = _contador("emision_verificacion_pago_total", "no_confirmado")
    pagos_stub.pagos["saga-1"]["estado"] = "REEMBOLSADO"

    with pytest.raises(ServiceError):
        await servicio.emitir("saga-1", "pg-1", butacas)

    assert _contador("emision_verificacion_pago_total", "no_confirmado") == antes + 1


async def test_las_metricas_cuentan_un_pagos_inalcanzable(servicio, butacas, pagos_stub):
    from .fakes import error_de_pagos

    antes = _contador("emision_verificacion_pago_total", "pagos_inalcanzable")
    pagos_stub.error = error_de_pagos()

    with pytest.raises(ServiceError):
        await servicio.emitir("saga-1", "pg-1", butacas)

    assert _contador("emision_verificacion_pago_total", "pagos_inalcanzable") == antes + 1


async def test_las_metricas_cuentan_un_pago_distinto(servicio, butacas):
    antes = _contador("emision_verificacion_pago_total", "pago_distinto")

    with pytest.raises(ServiceError):
        await servicio.emitir("saga-1", "pg-ajeno", butacas)

    assert _contador("emision_verificacion_pago_total", "pago_distinto") == antes + 1


async def test_las_metricas_cuentan_la_anulacion(servicio, butacas):
    antes = _contador("emision_entradas_total", "anulada")

    await servicio.emitir("saga-1", "pg-1", butacas)
    await servicio.anular("saga-1")

    assert _contador("emision_entradas_total", "anulada") == antes + 1


async def test_un_falla_inyectada_impide_emitir(sesiones, cliente_pagos, butacas):
    """El control plane de los scripts de demo puede forzar el fallo."""
    fallos = FaultController()
    fallos.configurar("down", 0, 0.0)
    servicio = EmisionService(sesiones, cliente_pagos, faults=fallos)

    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-1", butacas)

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
    assert _entradas(sesiones) == []


async def test_un_modo_de_falla_desconocido_se_rechaza():
    fallos = FaultController()

    with pytest.raises(ModoInvalido):
        fallos.configurar("roto", 0, 0.0)
