"""Tests of the gRPC adapter of the `compras` service.

The adapter is the boundary with the rest of the system: it translates the
domain answer into the protobuf message and the domain error into a gRPC
status plus the machine readable code of `common.errors`.
"""

from __future__ import annotations

import json

import grpc
import pytest

import compras_pb2
from common.errors import CODES, ServiceError

from ..grpc_service import ComprasServicer


class ErrorAbortado(Exception):
    """The error raised by `context.abort` of the real gRPC."""

    def __init__(self, codigo, detalles):
        self.codigo = codigo
        self.detalles = detalles
        super().__init__(str(codigo))


class ContextoFalso:
    """Context of a gRPC call that records the abort."""

    def __init__(self):
        self.codigo = None
        self.detalles = None

    async def abort(self, codigo, detalles):
        self.codigo = codigo
        self.detalles = detalles
        raise ErrorAbortado(codigo, detalles)


class ServicioFalso:
    """Domain service with a fixed answer."""

    def __init__(self, resultado=None, error=None):
        self.resultado = resultado
        self.error = error
        self.llamadas = []

    async def iniciar_compra(self, **kwargs):
        self.llamadas.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.resultado

    async def consultar_compra(self, saga_id):
        self.llamadas.append({"saga_id": saga_id})
        if self.error is not None:
            raise self.error
        return self.resultado


def _compra_de_ejemplo() -> dict:
    return {
        "saga_id": "saga-1",
        "estado": "CONFIRMADA",
        "pasos": [
            {
                "nombre": "RESERVAR",
                "resultado": "OK",
                "detalle": "2 butacas reservadas",
                "iniciado_en": "2026-11-20T10:00:00+00:00",
                "finalizado_en": "2026-11-20T10:00:01+00:00",
            },
            {
                "nombre": "COBRAR",
                "resultado": "OK",
                "detalle": "pago pg-1",
                "iniciado_en": "2026-11-20T10:00:01+00:00",
                "finalizado_en": "2026-11-20T10:00:02+00:00",
            },
        ],
        "compensaciones": [],
        "evento_id": "ev-1",
        "seccion_id": "sec-1",
        "cantidad": 2,
        "cliente_id": "cli-1",
        "error": "",
        "actualizada_en": "2026-11-20T10:00:03+00:00",
        "monto_total_centavos": 50_000_000,
        "moneda": "COP",
        "idempotente": False,
    }


async def test_la_compra_se_mapea_al_proto():
    servicer = ComprasServicer(ServicioFalso(resultado=_compra_de_ejemplo()))
    contexto = ContextoFalso()
    peticion = compras_pb2.IniciarCompraRequest(
        saga_id="saga-1",
        evento_id="ev-1",
        seccion_id="sec-1",
        butaca_ids=["sec-1-A1", "sec-1-A2"],
        cliente_id="cli-1",
        moneda="COP",
    )

    respuesta = await servicer.IniciarCompra(peticion, contexto)

    assert respuesta.compra.saga_id == "saga-1"
    assert respuesta.compra.estado == "CONFIRMADA"
    assert respuesta.compra.moneda == "COP"
    assert respuesta.compra.monto_total_centavos == 50_000_000
    assert [paso.nombre for paso in respuesta.compra.pasos] == ["RESERVAR", "COBRAR"]
    assert respuesta.compra.pasos[0].resultado == "OK"
    assert respuesta.idempotente is False


async def test_una_compra_idempotente_lo_dice():
    resultado = _compra_de_ejemplo()
    resultado["idempotente"] = True
    servicer = ComprasServicer(ServicioFalso(resultado=resultado))

    respuesta = await servicer.IniciarCompra(compras_pb2.IniciarCompraRequest(), ContextoFalso())

    assert respuesta.idempotente is True


async def test_un_error_de_dominio_aborta_con_el_codigo_maquina():
    servicer = ComprasServicer(
        ServicioFalso(error=ServiceError(CODES.INVALID, "el saga_id es obligatorio"))
    )
    contexto = ContextoFalso()

    with pytest.raises(ErrorAbortado):
        await servicer.IniciarCompra(compras_pb2.IniciarCompraRequest(), contexto)

    assert contexto.codigo == grpc.StatusCode.INVALID_ARGUMENT
    detalles = json.loads(contexto.detalles)
    assert detalles["code"] == CODES.INVALID


async def test_consultar_una_saga_devuelve_la_compra():
    servicer = ComprasServicer(ServicioFalso(resultado=_compra_de_ejemplo()))

    respuesta = await servicer.ConsultarCompra(
        compras_pb2.ConsultarCompraRequest(saga_id="saga-1"), ContextoFalso()
    )

    assert respuesta.compra.saga_id == "saga-1"
    assert respuesta.compra.estado == "CONFIRMADA"
