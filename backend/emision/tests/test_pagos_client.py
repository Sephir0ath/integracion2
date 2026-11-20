"""Tests of the client of the `pagos` service.

They check the two things that the rule of the system needs from the
dependency: every call carries the deadline of the configuration
(`GRPC_DEADLINE`) and a failure of the dependency is never mistaken for
a confirmation.
"""

from __future__ import annotations

import grpc
import pytest

from common.config import Settings
from common.errors import CODES, ServiceError

from ..pagos_client import PagosClient
from .fakes import FakePagosStub, error_de_pagos


def _cliente(stub) -> PagosClient:
    return PagosClient(Settings.from_env(), stub=stub)


async def test_la_llamada_lleva_el_limite_de_espera(monkeypatch, pagos_stub):
    """Toda llamada entre servicios tiene un limite de espera."""
    monkeypatch.setenv("GRPC_DEADLINE", "2.5")

    cliente = _cliente(pagos_stub)
    await cliente.consultar_pago("saga-1")

    assert pagos_stub.calls[0]["timeout"] == 2.5
    assert pagos_stub.calls[0]["timeout"] == Settings.from_env().grpc_deadline


async def test_la_llamada_lleva_el_limite_por_defecto(pagos_stub):
    cliente = _cliente(pagos_stub)

    await cliente.consultar_pago("saga-1")

    assert pagos_stub.calls[0]["timeout"] == Settings.from_env().grpc_deadline
    assert pagos_stub.calls[0]["timeout"] > 0


async def test_el_pago_de_la_saga_se_lee(pagos_stub):
    cliente = _cliente(pagos_stub)

    pago = await cliente.consultar_pago("saga-1")

    assert pago["pago_id"] == "pg-1"
    assert pago["estado"] == "CONFIRMADO"
    assert pago["saga_id"] == "saga-1"


async def test_una_saga_sin_pago_es_not_found(pagos_stub):
    cliente = _cliente(pagos_stub)

    with pytest.raises(ServiceError) as error:
        await cliente.consultar_pago("saga-9")

    assert error.value.code == CODES.NOT_FOUND


async def test_un_pagos_inalcanzable_es_dependencia_no_disponible(pagos_stub):
    pagos_stub.error = error_de_pagos()
    cliente = _cliente(pagos_stub)

    with pytest.raises(ServiceError) as error:
        await cliente.consultar_pago("saga-1")

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE


async def test_un_pagos_que_no_responde_a_tiempo_es_dependencia_no_disponible(pagos_stub):
    pagos_stub.error = error_de_pagos(
        grpc.StatusCode.DEADLINE_EXCEEDED, "el limite de espera expiro"
    )
    cliente = _cliente(pagos_stub)

    with pytest.raises(ServiceError) as error:
        await cliente.consultar_pago("saga-1")

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
