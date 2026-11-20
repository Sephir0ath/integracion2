"""Fakes used by the tests of the `emision` service.

The tests run without PostgreSQL and without the `pagos` service running:
the verification of the payment uses a fake stub with the same
`ConsultarPago` method of `pagos.PagosService`, injected where the client
of the dependency is built. That stub records the deadline it receives,
so the test can prove that every call between services carries its time
limit, and it can fail the way an unreachable dependency fails.

The fake answers like the real service: a saga without payment is
`NOT_FOUND`, a saga with payment answers the payment and its state.
"""

from __future__ import annotations

import grpc
import pagos_pb2


def error_de_pagos(
    codigo: grpc.StatusCode = grpc.StatusCode.UNAVAILABLE,
    detalle: str = "el servicio de pagos no esta disponible",
) -> grpc.aio.AioRpcError:
    """The error of a `pagos` service that cannot answer."""
    return grpc.aio.AioRpcError(codigo, None, None, details=detalle)


class FakePagosStub:
    """Stub of `pagos.PagosService` with only `ConsultarPago`."""

    def __init__(self, pagos: dict | None = None, error: Exception | None = None, probe=None) -> None:
        # `pagos` maps a saga to its payment: `{"saga-1": {"pago_id": ...,
        # "estado": "CONFIRMADO"}}`. A saga that is not in the map has no
        # payment, exactly like in the real service.
        self.pagos = dict(
            pagos
            if pagos is not None
            else {
                "saga-1": {"pago_id": "pg-1", "estado": "CONFIRMADO"},
                "saga-2": {"pago_id": "pg-2", "estado": "CONFIRMADO"},
            }
        )
        self.error = error
        # `probe` is called on every request: it lets a test look at the
        # state of the system while the dependency is being consulted.
        self.probe = probe
        self.calls: list[dict] = []

    async def ConsultarPago(self, request, timeout=None):
        """Answer the payment of the saga and record the deadline received."""
        self.calls.append({"saga_id": request.saga_id, "timeout": timeout})
        if self.probe is not None:
            self.probe()
        if self.error is not None:
            raise self.error
        pago = self.pagos.get(request.saga_id)
        if pago is None:
            raise error_de_pagos(
                grpc.StatusCode.NOT_FOUND,
                f"no hay pago para la saga '{request.saga_id}'",
            )
        mensaje = pagos_pb2.Pago(
            pago_id=pago.get("pago_id", ""),
            saga_id=request.saga_id,
            monto_centavos=int(pago.get("monto_centavos", 0)),
            moneda=pago.get("moneda", "COP"),
            estado=pago.get("estado", ""),
        )
        return pagos_pb2.PagoResponse(pago=mensaje, estado=mensaje.estado, idempotente=False)
