"""Ticket emission service of the ticketera domain.

The service owns the issued tickets (`emision` database) and it is the
place where the saga step `EMITIR` and its compensation `ANULAR` are
implemented, both of them idempotent.

The rule of the system lives here:

    *No se emiten entradas sin cobro exitoso.*

It is enforced by asking the `pagos` service, which owns the ledger: a
ticket is written only when the payment of the saga exists, is
`CONFIRMADO` and is the very payment that the caller names in the
request. A `pagos` that cannot be reached is not a confirmation either,
so the answer is `DEPENDENCY_UNAVAILABLE`.
"""

from __future__ import annotations
