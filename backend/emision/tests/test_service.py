"""Tests of the domain logic of the `emision` service.

They cover the rule of the system ("no se emiten entradas sin cobro
exitoso"), the idempotency of the saga step `EMITIR` and of its
compensation `ANULAR`, and the QR payload of the ticket. The database is
a temporary SQLite file and the payment verification uses a fake stub, so
the tests need neither PostgreSQL nor a running `pagos` service.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from common.errors import CODES, ServiceError

from ..models import ANULADA, EMITIDA, Entrada
from ..service import calcular_qr, verificar_qr


def _entradas(sesiones) -> list[Entrada]:
    """The tickets of the database, as they are committed."""
    with sesiones() as session:
        return list(session.execute(select(Entrada)).scalars().all())


# ------------------------------------------------------- el cobro primero --
async def test_emitir_crea_una_entrada_por_butaca(servicio, butacas, sesiones):
    resultado = await servicio.emitir("saga-1", "pg-1", butacas)

    assert resultado["estado"] == EMITIDA
    assert resultado["idempotente"] is False
    assert len(resultado["entradas"]) == 2
    assert [entrada["butaca_id"] for entrada in resultado["entradas"]] == [
        "sec-1-A1",
        "sec-1-A2",
    ]
    assert all(entrada["estado"] == EMITIDA for entrada in resultado["entradas"])
    assert all(entrada["fila"] == "A" for entrada in resultado["entradas"])
    assert [entrada["numero"] for entrada in resultado["entradas"]] == [1, 2]
    assert all(entrada["qr"] for entrada in resultado["entradas"])

    guardadas = _entradas(sesiones)
    assert len(guardadas) == 2
    assert all(entrada.estado == EMITIDA for entrada in guardadas)
    assert all(entrada.saga_id == "saga-1" for entrada in guardadas)


async def test_emitir_consulta_el_pago_antes_de_escribir(servicio, butacas, pagos_stub, sesiones):
    await servicio.emitir("saga-1", "pg-1", butacas)

    assert pagos_stub.calls == [{"saga_id": "saga-1", "timeout": pagos_stub.calls[0]["timeout"]}]
    assert pagos_stub.calls[0]["saga_id"] == "saga-1"
    assert len(_entradas(sesiones)) == 2


@pytest.mark.parametrize("estado", ["REEMBOLSADO", "FALLIDO"])
async def test_emitir_con_pago_no_confirmado_no_crea_entradas(
    servicio, butacas, pagos_stub, sesiones, estado
):
    pagos_stub.pagos["saga-1"]["estado"] = estado

    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-1", butacas)

    assert error.value.code == CODES.PAYMENT_REQUIRED
    assert _entradas(sesiones) == [], "no puede quedar una entrada sin cobro"


async def test_emitir_sin_pago_de_la_saga_no_crea_entradas(servicio, butacas, sesiones):
    """Una saga sin pago no tiene cobro exitoso: tampoco puede emitir."""
    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-9", "pg-9", butacas)

    assert error.value.code == CODES.PAYMENT_REQUIRED
    assert _entradas(sesiones) == []


async def test_emitir_con_otro_pago_no_crea_entradas(servicio, butacas, sesiones):
    """El pago que se nombra tiene que ser el confirmado de la saga."""
    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-ajeno", butacas)

    assert error.value.code == CODES.PAYMENT_REQUIRED
    assert _entradas(sesiones) == [], "no puede quedar una entrada cobrada con otro pago"


async def test_emitir_con_pagos_inalcanzable_no_crea_entradas(
    servicio, butacas, pagos_stub, sesiones
):
    """Un servicio de pagos que no responde no es una confirmación."""
    from .fakes import error_de_pagos

    pagos_stub.error = error_de_pagos()

    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-1", butacas)

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
    assert _entradas(sesiones) == []


# ----------------------------------------------------------- idempotencia --
async def test_emitir_repetido_devuelve_las_mismas_entradas(servicio, butacas, sesiones):
    primera = await servicio.emitir("saga-1", "pg-1", butacas)
    segunda = await servicio.emitir("saga-1", "pg-1", butacas)

    assert primera["idempotente"] is False
    assert segunda["idempotente"] is True
    assert segunda["entradas"] == primera["entradas"]
    assert len(_entradas(sesiones)) == 2, "no puede haber una segunda entrada para la misma butaca"


async def test_emitir_repetido_con_otra_clave_no_duplica_la_butaca(servicio, butacas, sesiones):
    """La clave de idempotencia por defecto es la de la saga y el paso."""
    await servicio.emitir("saga-1", "pg-1", butacas)
    await servicio.emitir("saga-1", "pg-1", butacas, idempotency_key="saga-1:EMITIR")

    guardadas = _entradas(sesiones)
    assert len(guardadas) == 2
    assert len({entrada.butaca_id for entrada in guardadas}) == 2


async def test_la_misma_clave_con_otro_payload_es_conflicto(servicio, butacas, sesiones):
    await servicio.emitir("saga-1", "pg-1", butacas)

    otras = [
        {"butaca_id": "sec-1-B1", "seccion_id": "sec-1", "fila": "B", "numero": 1},
    ]
    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-1", otras, idempotency_key="saga-1:EMITIR")

    assert error.value.code == CODES.CONFLICT_IDEMPOTENCIA
    assert len(_entradas(sesiones)) == 2, "un conflicto no puede emitir nada"


async def test_una_saga_sin_butacas_es_invalida(servicio):
    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-1", [])

    assert error.value.code == CODES.INVALID


async def test_la_misma_butaca_dos_veces_es_invalida(servicio, butacas):
    repetida = list(butacas) + [butacas[0]]

    with pytest.raises(ServiceError) as error:
        await servicio.emitir("saga-1", "pg-1", repetida)

    assert error.value.code == CODES.INVALID


# --------------------------------------------------------------- anulacion --
async def test_anular_anula_las_entradas(servicio, butacas, sesiones):
    await servicio.emitir("saga-1", "pg-1", butacas)

    resultado = await servicio.anular("saga-1")

    assert resultado["estado"] == ANULADA
    assert resultado["idempotente"] is False
    assert all(entrada["estado"] == ANULADA for entrada in resultado["entradas"])
    assert all(entrada.anulada_en for entrada in _entradas(sesiones))


async def test_anular_es_idempotente(servicio, butacas, sesiones):
    await servicio.emitir("saga-1", "pg-1", butacas)

    primera = await servicio.anular("saga-1")
    segunda = await servicio.anular("saga-1")

    assert primera["idempotente"] is False
    assert segunda["idempotente"] is True
    assert segunda["entradas"] == primera["entradas"]
    assert segunda["estado"] == ANULADA
    assert all(entrada.estado == ANULADA for entrada in _entradas(sesiones))


async def test_anular_no_necesita_un_cobro_confirmado(servicio, butacas, pagos_stub):
    """Anular es la compensacion: no depende del estado del pago."""
    await servicio.emitir("saga-1", "pg-1", butacas)
    pagos_stub.pagos["saga-1"]["estado"] = "REEMBOLSADO"

    resultado = await servicio.anular("saga-1")

    assert resultado["estado"] == ANULADA
    assert all(entrada["estado"] == ANULADA for entrada in resultado["entradas"])


async def test_anular_una_saga_sin_entradas_no_es_error(servicio):
    resultado = await servicio.anular("saga-9")

    assert resultado["estado"] == "NO_ENCONTRADA"
    assert resultado["entradas"] == []


async def test_anular_no_toca_las_entradas_de_otra_saga(servicio, butacas, sesiones):
    await servicio.emitir("saga-1", "pg-1", butacas)

    await servicio.anular("saga-1")

    otras = await servicio.emitir("saga-2", "pg-2", butacas)
    assert all(entrada["estado"] == EMITIDA for entrada in otras["entradas"])


# ---------------------------------------------------------------- consulta --
async def test_listar_devuelve_las_entradas_con_su_estado(servicio, butacas):
    await servicio.emitir("saga-1", "pg-1", butacas)

    resultado = servicio.listar("saga-1")

    assert resultado["estado"] == EMITIDA
    assert len(resultado["entradas"]) == 2
    assert [entrada["butaca_id"] for entrada in resultado["entradas"]] == ["sec-1-A1", "sec-1-A2"]

    await servicio.anular("saga-1")
    anuladas = servicio.listar("saga-1")
    assert anuladas["estado"] == ANULADA
    assert all(entrada["estado"] == ANULADA for entrada in anuladas["entradas"])


async def test_listar_una_saga_sin_entradas_no_encuentra_nada(servicio):
    resultado = servicio.listar("saga-9")

    assert resultado["estado"] == "NO_ENCONTRADA"
    assert resultado["entradas"] == []


# --------------------------------------------------------------------- qr --
def test_el_qr_es_determinista():
    assert calcular_qr("saga-1", "sec-1-A1") == calcular_qr("saga-1", "sec-1-A1")
    assert calcular_qr("saga-1", "sec-1-A1") != calcular_qr("saga-1", "sec-1-A2")
    assert calcular_qr("saga-1", "sec-1-A1") != calcular_qr("saga-2", "sec-1-A1")


def test_el_checksum_del_qr_se_puede_recalcular():
    qr = calcular_qr("saga-1", "sec-1-A1")

    assert qr.startswith("TKT-saga-1-sec-1-A1-")
    assert verificar_qr(qr) is True
    assert verificar_qr(qr, "saga-1", "sec-1-A1") is True


def test_un_qr_alterado_no_verifica():
    qr = calcular_qr("saga-1", "sec-1-A1")
    alterado = qr[:-1] + ("0" if qr[-1] != "0" else "1")

    assert verificar_qr(alterado) is False
    assert verificar_qr("no-es-un-qr") is False


async def test_el_qr_de_la_entrada_emitida_verifica(servicio, butacas):
    resultado = await servicio.emitir("saga-1", "pg-1", butacas)

    for entrada in resultado["entradas"]:
        assert verificar_qr(entrada["qr"], "saga-1", entrada["butaca_id"]) is True
