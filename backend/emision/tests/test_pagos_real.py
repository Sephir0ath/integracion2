"""Tests of the client of `pagos` against a real gRPC server.

They run the `pagos` service in the same process (over a real channel of
`grpc.aio`), so the whole path of the outbound call is exercised: the
deadline, the answer of the dependency and the translation of its
failures. They need neither PostgreSQL nor a running `pagos` service.
"""

from __future__ import annotations

import grpc
import pagos_pb2
import pagos_pb2_grpc
import pytest

from common.config import Settings
from common.errors import CODES, ServiceError

from ..pagos_client import PagosClient


class ServidorPagos(pagos_pb2_grpc.PagosServiceServicer):
    """The `pagos` service, running in the same process."""

    def __init__(self) -> None:
        self.llamadas: list[str] = []

    async def ConsultarPago(self, request, context):
        self.llamadas.append(request.saga_id)
        return pagos_pb2.PagoResponse(
            pago=pagos_pb2.Pago(
                pago_id="pg-real",
                saga_id=request.saga_id,
                monto_centavos=50000,
                moneda="COP",
                estado="CONFIRMADO",
            ),
            estado="CONFIRMADO",
        )


@pytest.fixture()
async def pagos_real():
    """A real `pagos` server, listening in a free port of the loopback."""
    servidor = grpc.aio.server()
    servicio = ServidorPagos()
    pagos_pb2_grpc.add_PagosServiceServicer_to_server(servicio, servidor)
    puerto = servidor.add_insecure_port("127.0.0.1:0")
    await servidor.start()
    try:
        yield f"127.0.0.1:{puerto}", servicio
    finally:
        await servidor.stop(None)


async def test_el_cliente_habla_con_el_pagos_real(pagos_real):
    direccion, servicio = pagos_real
    cliente = PagosClient(Settings(pagos_target=direccion, grpc_deadline=2.0))

    try:
        pago = await cliente.consultar_pago("saga-1")
    finally:
        await cliente.close()

    assert pago["pago_id"] == "pg-real"
    assert pago["estado"] == "CONFIRMADO"
    assert pago["saga_id"] == "saga-1"
    assert servicio.llamadas == ["saga-1"]


async def test_un_pagos_caido_es_dependencia_no_disponible():
    """Un destino que no escucha no es una confirmacion."""
    cliente = PagosClient(Settings(pagos_target="127.0.0.1:1", grpc_deadline=2.0))

    with pytest.raises(ServiceError) as error:
        await cliente.consultar_pago("saga-1")

    assert error.value.code == CODES.DEPENDENCY_UNAVAILABLE
    await cliente.close()
