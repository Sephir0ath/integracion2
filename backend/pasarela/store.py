"""Ledger of the simulated gateway.

The simulator has no database: it keeps its payments in memory, which is
exactly what a third party provider is for the system under test (the
`pagos` service never reads it, it only asks for a charge or a refund).

The ledger is idempotent by `idempotency_key`: presenting the same key
again returns the very same `pago_id` and never charges twice. That is what
makes a retry of the `COBRAR` saga step safe.
"""

from __future__ import annotations

import uuid

CONFIRMADO = "CONFIRMADO"
REEMBOLSADO = "REEMBOLSADO"


class PasarelaStore:
    """In memory payments of the simulated provider."""

    def __init__(self) -> None:
        self._pagos: dict[str, dict] = {}
        self._por_clave: dict[str, str] = {}
        self.cobros = 0
        self.reembolsos = 0
        self.llamadas = 0

    def cobrar(self, monto_centavos: int, idempotency_key: str) -> dict:
        """Charge an amount. The same key always returns the same payment."""
        self.llamadas += 1
        clave = idempotency_key or ""
        existente = self._por_clave.get(clave)
        if existente is not None:
            # Replay: the same payment, charged only once.
            return dict(self._pagos[existente])

        pago_id = f"pgw_{uuid.uuid4().hex}"
        referencia = f"REF-{uuid.uuid4().hex[:10].upper()}"
        registro = {
            "pago_id": pago_id,
            "estado": CONFIRMADO,
            "referencia": referencia,
            "monto_centavos": int(monto_centavos),
            "idempotency_key": clave,
        }
        self._pagos[pago_id] = registro
        self._por_clave[clave] = pago_id
        self.cobros += 1
        return dict(registro)

    def reembolsar(self, pago_id: str) -> dict | None:
        """Refund a payment. Returns None when the payment is unknown."""
        self.llamadas += 1
        registro = self._pagos.get(pago_id)
        if registro is None:
            return None
        registro["estado"] = REEMBOLSADO
        self.reembolsos += 1
        return dict(registro)

    def obtener(self, pago_id: str) -> dict | None:
        """Read a payment without changing it."""
        registro = self._pagos.get(pago_id)
        return dict(registro) if registro is not None else None

    def pagos(self) -> dict:
        """Snapshot of the ledger, used by the health endpoint."""
        return dict(self._pagos)
