"""Fakes of the three domain services, used by the tests of the orchestrator.

The tests of the `compras` service run without PostgreSQL and without the
domain services running: the dependencies are replaced by these fakes.

Every fake records its calls in a **shared registry**, so a test can
assert the order of the calls and not only that they happened. That order
is the core of the saga: the compensations have to run in the reverse
order of the confirmed steps (`REEMBOLSAR` before `LIBERAR`), and that is
only observable if the calls are recorded in sequence.

The fakes fail the way a real dependency fails: with a `ServiceError`
that carries the code of `common.errors`.
"""

from __future__ import annotations

import asyncio

from common.errors import CODES, ServiceError


class LlamadasFake:
    """Shared registry of the calls made to the dependencies."""

    def __init__(self) -> None:
        self.registro: list[tuple[str, str, str | None]] = []

    def anotar(self, nombre: str, saga_id: str, clave: str | None = None) -> None:
        """Record one call."""
        self.registro.append((nombre, saga_id, clave))

    @property
    def nombres(self) -> list[str]:
        """The names of the calls, in order."""
        return [nombre for nombre, _, _ in self.registro]

    def de(self, nombre: str) -> list[tuple[str, str, str | None]]:
        """The calls of one method, in order."""
        return [llamada for llamada in self.registro if llamada[0] == nombre]


class _FalsoBase:
    """Common behaviour of the fakes: the registry and the failure."""

    def __init__(self, llamadas: LlamadasFake, error: Exception | None = None, delay: float = 0.0) -> None:
        self.llamadas = llamadas
        self.error = error
        self.delay = delay

    async def _responder(self, nombre: str, saga_id: str, clave: str | None):
        """Record the call, wait the configured delay and raise the failure."""
        self.llamadas.anotar(nombre, saga_id, clave)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error


class FakeButacas(_FalsoBase):
    """Fake of `butacas.ButacasService`."""

    def __init__(
        self,
        llamadas: LlamadasFake,
        error: Exception | None = None,
        delay: float = 0.0,
        precio_centavos: int = 25_000_000,
        error_confirmar: Exception | None = None,
    ) -> None:
        super().__init__(llamadas, error, delay)
        self.precio_centavos = precio_centavos
        self.error_confirmar = error_confirmar

    async def reservar(
        self,
        saga_id: str,
        evento_id: str,
        seccion_id: str,
        butaca_ids: list[str],
        idempotency_key: str | None = None,
    ) -> dict:
        await self._responder("butacas.reservar", saga_id, idempotency_key)
        butacas = [
            {
                "butaca_id": butaca_id,
                "seccion_id": seccion_id,
                "fila": "A",
                "numero": indice + 1,
                "estado": "RESERVADA",
                "saga_id": saga_id,
            }
            for indice, butaca_id in enumerate(butaca_ids)
        ]
        return {
            "reserva": {
                "reserva_id": f"rsv-{saga_id}",
                "saga_id": saga_id,
                "seccion_id": seccion_id,
                "estado": "ACTIVA",
                "precio_centavos": self.precio_centavos,
                "creada_en": "2026-11-20T00:00:00+00:00",
                "butacas": butacas,
            },
            "butacas": butacas,
            "estado": "ACTIVA",
        }

    async def liberar(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        await self._responder("butacas.liberar", saga_id, idempotency_key)
        return {"liberado": True, "butacas_liberadas": 2, "estado": "LIBERADA"}

    async def confirmar_venta(self, saga_id: str) -> dict:
        await self._responder("butacas.confirmar_venta", saga_id, None)
        if self.error_confirmar is not None:
            raise self.error_confirmar
        return {"confirmada": True, "butacas_confirmadas": 2, "estado": "VENDIDA"}


class FakePagos(_FalsoBase):
    """Fake of `pagos.PagosService`."""

    def __init__(
        self,
        llamadas: LlamadasFake,
        error: Exception | None = None,
        delay: float = 0.0,
        error_reembolso: Exception | None = None,
    ) -> None:
        super().__init__(llamadas, error, delay)
        self.error_reembolso = error_reembolso
        self.cobros: list[dict] = []

    async def cobrar(
        self,
        saga_id: str,
        monto_centavos: int,
        moneda: str,
        idempotency_key: str | None = None,
    ) -> dict:
        self.cobros.append(
            {"saga_id": saga_id, "monto_centavos": monto_centavos, "moneda": moneda}
        )
        await self._responder("pagos.cobrar", saga_id, idempotency_key)
        return {
            "pago": {
                "pago_id": f"pg-{saga_id}",
                "saga_id": saga_id,
                "monto_centavos": monto_centavos,
                "moneda": moneda,
                "estado": "CONFIRMADO",
                "pasarela_ref": f"pas-{saga_id}",
            },
            "estado": "CONFIRMADO",
            "idempotente": False,
        }

    async def reembolsar(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        await self._responder("pagos.reembolsar", saga_id, idempotency_key)
        if self.error_reembolso is not None:
            raise self.error_reembolso
        return {
            "pago": {
                "pago_id": f"pg-{saga_id}",
                "saga_id": saga_id,
                "monto_centavos": 0,
                "moneda": "COP",
                "estado": "REEMBOLSADO",
                "pasarela_ref": f"pas-{saga_id}",
            },
            "estado": "REEMBOLSADO",
            "idempotente": False,
        }


class FakeEmision(_FalsoBase):
    """Fake of `emision.EmisionService`."""

    def __init__(
        self,
        llamadas: LlamadasFake,
        error: Exception | None = None,
        delay: float = 0.0,
        error_anular: Exception | None = None,
    ) -> None:
        super().__init__(llamadas, error, delay)
        self.error_anular = error_anular
        self.emitidos: list[dict] = []

    async def emitir(
        self,
        saga_id: str,
        pago_id: str,
        butacas: list[dict],
        idempotency_key: str | None = None,
    ) -> dict:
        self.emitidos.append({"saga_id": saga_id, "pago_id": pago_id, "butacas": butacas})
        await self._responder("emision.emitir", saga_id, idempotency_key)
        return {
            "entradas": [
                {
                    "entrada_id": f"ent-{saga_id}-{indice + 1}",
                    "saga_id": saga_id,
                    "butaca_id": butaca["butaca_id"],
                    "fila": butaca.get("fila", ""),
                    "numero": int(butaca.get("numero", 0)),
                    "qr": f"TKT-{saga_id}-{butaca['butaca_id']}-00000000",
                    "estado": "EMITIDA",
                }
                for indice, butaca in enumerate(butacas)
            ],
            "estado": "EMITIDA",
            "idempotente": False,
        }

    async def anular(self, saga_id: str, idempotency_key: str | None = None) -> dict:
        await self._responder("emision.anular", saga_id, idempotency_key)
        if self.error_anular is not None:
            raise self.error_anular
        return {"entradas": [], "estado": "ANULADA", "idempotente": False}


def error_de_dependencia(codigo: str = CODES.DEPENDENCY_UNAVAILABLE) -> ServiceError:
    """The failure of a dependency that cannot answer."""
    return ServiceError(codigo, f"la dependencia fallo con {codigo}", details={"origen": "fake"})
