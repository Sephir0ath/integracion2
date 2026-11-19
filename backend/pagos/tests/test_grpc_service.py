"""Tests of the gRPC adapter of the `pagos` service.

They check that the domain errors reach the caller as a gRPC status plus a
machine readable code, and that the answers of the domain are translated
into the protobuf messages of `proto/pagos.proto`.
"""

from __future__ import annotations

import json

import grpc
import pytest

import pagos_pb2
from common.errors import CODES, ServiceError

from ..grpc_service import PagosServicer
from ..service import PagosService
from .fakes import FakePasarela, FakeSessionProvider


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


def _servicio(pasarela: FakePasarela | None = None) -> PagosService:
    return PagosService(FakeSessionProvider(), pasarela or FakePasarela())


@pytest.fixture()
def servicer():
    return PagosServicer(_servicio())


async def test_cobrar_devuelve_el_pago_confirmado(servicer):
    respuesta = await servicer.Cobrar(
        pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=25000, moneda="COP"), FakeContext()
    )

    assert respuesta.estado == "CONFIRMADO"
    assert respuesta.idempotente is False
    assert respuesta.pago.saga_id == "saga-1"
    assert respuesta.pago.monto_centavos == 25000
    assert respuesta.pago.pasarela_ref == "pgw_falso"


async def test_un_cobro_repetido_es_idempotente(servicer):
    peticion = pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=25000, moneda="COP")

    primera = await servicer.Cobrar(peticion, FakeContext())
    segunda = await servicer.Cobrar(peticion, FakeContext())

    assert primera.idempotente is False
    assert segunda.idempotente is True
    assert segunda.pago.pago_id == primera.pago.pago_id


async def test_un_error_de_dominio_se_traduce_a_estado_grpc_y_codigo():
    pasarela = FakePasarela(error=ServiceError(CODES.CIRCUIT_OPEN, "degradado: pasarela no disponible"))
    servicer = PagosServicer(_servicio(pasarela))
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.Cobrar(
            pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=25000), contexto
        )

    assert contexto.code == grpc.StatusCode.UNAVAILABLE
    cuerpo = json.loads(contexto.details)
    assert cuerpo["code"] == CODES.CIRCUIT_OPEN
    assert "pasarela" in cuerpo["message"]


async def test_un_conflicto_de_idempotencia_se_traduce_a_grpc(servicer):
    peticion = pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=25000)
    await servicer.Cobrar(peticion, FakeContext())

    contexto = FakeContext()
    with pytest.raises(AbortError):
        await servicer.Cobrar(
            pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=99000), contexto
        )

    assert contexto.code == grpc.StatusCode.ABORTED
    assert json.loads(contexto.details)["code"] == CODES.CONFLICT_IDEMPOTENCIA


async def test_un_pago_inexistente_se_traduce_a_not_found(servicer):
    contexto = FakeContext()

    with pytest.raises(AbortError):
        await servicer.ConsultarPago(pagos_pb2.ConsultarPagoRequest(saga_id="saga-9"), contexto)

    assert contexto.code == grpc.StatusCode.NOT_FOUND


async def test_consultar_un_pago_devuelve_el_pago(servicer):
    await servicer.Cobrar(
        pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=25000, moneda="COP"), FakeContext()
    )

    respuesta = await servicer.ConsultarPago(pagos_pb2.ConsultarPagoRequest(saga_id="saga-1"), FakeContext())

    assert respuesta.estado == "CONFIRMADO"
    assert respuesta.pago.monto_centavos == 25000


async def test_reembolsar_devuelve_el_pago_reembolsado(servicer):
    await servicer.Cobrar(
        pagos_pb2.CobrarRequest(saga_id="saga-1", monto_centavos=25000), FakeContext()
    )

    respuesta = await servicer.Reembolsar(pagos_pb2.ReembolsarRequest(saga_id="saga-1"), FakeContext())

    assert respuesta.estado == "REEMBOLSADO"


async def test_el_estado_del_circuito_se_expone(servicer):
    respuesta = await servicer.EstadoCircuito(pagos_pb2.EstadoCircuitoRequest(), FakeContext())

    assert respuesta.estado == "CLOSED"
    assert respuesta.fallos_consecutivos == 0
    assert respuesta.llamadas_rechazadas == 0
    assert respuesta.llamadas_en_curso == 0
    assert respuesta.rechazos_bulkhead == 0
    assert respuesta.fallback == ""


async def test_los_metodos_de_pago_se_listan(servicer):
    respuesta = await servicer.ListarMetodosPago(pagos_pb2.ListarMetodosPagoRequest(), FakeContext())

    assert respuesta.metodos
    assert respuesta.metodos[0].metodo
