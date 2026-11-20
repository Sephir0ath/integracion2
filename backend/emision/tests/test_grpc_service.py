"""Tests of the gRPC adapter of the `emision` service.

They check that the domain errors reach the caller as a gRPC status plus
a machine readable code, and that the answers of the domain are
translated into the protobuf messages of `proto/emision.proto`.
"""

from __future__ import annotations

import json

import grpc
import pytest

import emision_pb2
from common.errors import CODES

from ..grpc_service import EmisionServicer
from .fakes import error_de_pagos


class AbortError(Exception):
    """Raised by the fake context when the servicer aborts the call."""


class FakeContext:
    """Fake of the gRPC context: records the abort of the servicer."""

    def __init__(self) -> None:
        self.code = None
        self.details = None

    async def abort(self, code, details):
        self.code = code
        self.details = details
        raise AbortError(code, details)


@pytest.fixture()
def servicer(servicio):
    return EmisionServicer(servicio)


def _peticion(saga_id: str = "saga-1", pago_id: str = "pg-1"):
    return emision_pb2.EmitirRequest(
        saga_id=saga_id,
        pago_id=pago_id,
        butacas=[
            emision_pb2.ButacaRef(
                butaca_id="sec-1-A1", seccion_id="sec-1", fila="A", numero=1
            ),
            emision_pb2.ButacaRef(
                butaca_id="sec-1-A2", seccion_id="sec-1", fila="A", numero=2
            ),
        ],
        idempotency_key="saga-1:EMITIR",
    )


async def test_emitir_devuelve_las_entradas_emitidas(servicer):
    respuesta = await servicer.Emitir(_peticion(), FakeContext())

    assert respuesta.estado == "EMITIDA"
    assert respuesta.idempotente is False
    assert len(respuesta.entradas) == 2
    assert respuesta.entradas[0].butaca_id == "sec-1-A1"
    assert respuesta.entradas[0].fila == "A"
    assert respuesta.entradas[0].numero == 1
    assert respuesta.entradas[0].qr
    assert respuesta.entradas[0].estado == "EMITIDA"
    assert respuesta.entradas[0].saga_id == "saga-1"


async def test_emitir_repetido_es_idempotente(servicer):
    primera = await servicer.Emitir(_peticion(), FakeContext())
    segunda = await servicer.Emitir(_peticion(), FakeContext())

    assert primera.idempotente is False
    assert segunda.idempotente is True
    assert [e.entrada_id for e in segunda.entradas] == [e.entrada_id for e in primera.entradas]


async def test_un_pago_no_confirmado_se_traduce_a_grpc(servicer, pagos_stub):
    pagos_stub.pagos["saga-1"]["estado"] = "REEMBOLSADO"
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.Emitir(_peticion(), contexto)

    assert contexto.code == grpc.StatusCode.FAILED_PRECONDITION
    cuerpo = json.loads(contexto.details)
    assert cuerpo["code"] == CODES.PAYMENT_REQUIRED


async def test_un_pagos_inalcanzable_se_traduce_a_grpc(servicer, pagos_stub):
    pagos_stub.error = error_de_pagos()
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.Emitir(_peticion(), contexto)

    assert contexto.code == grpc.StatusCode.UNAVAILABLE
    assert json.loads(contexto.details)["code"] == CODES.DEPENDENCY_UNAVAILABLE


async def test_un_conflicto_de_idempotencia_se_traduce_a_grpc(servicer):
    await servicer.Emitir(_peticion(), FakeContext())
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.Emitir(_peticion(pago_id="pg-otro"), contexto)

    assert contexto.code == grpc.StatusCode.ABORTED
    assert json.loads(contexto.details)["code"] == CODES.CONFLICT_IDEMPOTENCIA


async def test_una_peticion_invalida_se_traduce_a_grpc(servicer):
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.Emitir(emision_pb2.EmitirRequest(saga_id="saga-1"), contexto)

    assert contexto.code == grpc.StatusCode.INVALID_ARGUMENT
    assert json.loads(contexto.details)["code"] == CODES.INVALID


async def test_anular_devuelve_las_entradas_anuladas(servicer):
    await servicer.Emitir(_peticion(), FakeContext())

    respuesta = await servicer.Anular(
        emision_pb2.AnularRequest(saga_id="saga-1", idempotency_key="saga-1:ANULAR"),
        FakeContext(),
    )

    assert respuesta.estado == "ANULADA"
    assert all(entrada.estado == "ANULADA" for entrada in respuesta.entradas)


async def test_anular_una_saga_sin_entradas_no_encuentra_nada(servicer):
    respuesta = await servicer.Anular(
        emision_pb2.AnularRequest(saga_id="saga-9", idempotency_key="saga-9:ANULAR"),
        FakeContext(),
    )

    assert respuesta.estado == "NO_ENCONTRADA"
    assert list(respuesta.entradas) == []


async def test_listar_devuelve_las_entradas_con_su_estado(servicer):
    await servicer.Emitir(_peticion(), FakeContext())

    respuesta = await servicer.Listar(
        emision_pb2.ListarEntradasRequest(saga_id="saga-1"), FakeContext()
    )

    assert respuesta.estado == "EMITIDA"
    assert len(respuesta.entradas) == 2
    assert all(entrada.estado == "EMITIDA" for entrada in respuesta.entradas)
