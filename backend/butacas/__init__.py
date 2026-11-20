"""Seat inventory service of the ticketera domain.

The service owns the inventory of the venues (`butacas` database): the
events, their sections and the seat rows. It implements the saga step
`RESERVAR` and its compensation `LIBERAR`, both idempotent, and it is the
place where the rule of the system lives:

    *Una butaca no se vende dos veces.*

That rule is enforced by the database and not by the application: the
reservation of a set of seats is one atomic statement
(`UPDATE ... RETURNING`) that claims only the seats that are still free and
tells exactly which ones it got. A `SELECT` followed by an `UPDATE` would
always leave a window open between the two statements.
"""

from __future__ import annotations
