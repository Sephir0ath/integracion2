"""Fault injection of the simulated gateway.

The control plane of the demo scripts lives here. The state is a plain
object shared by the whole process, so `GET /admin/fault` always answers
with the mode that is currently applied to the payment endpoints.

Semantics of the modes:

* ``ok``   the gateway behaves normally.
* ``slow`` every payment call is delayed ``delay_ms`` milliseconds, which is
  how a slow dependency is simulated (the delay is longer than the timeout
  of the caller).
* ``down`` every payment call fails with ``503``.

``fail_rate`` is orthogonal to the mode: it is the fraction of calls that
fail randomly, between ``0.0`` and ``1.0``.
"""

from __future__ import annotations

import asyncio
import random

MODOS = ("ok", "slow", "down")


class ModoInvalido(ValueError):
    """Raised when the requested fault mode is not one of the known ones."""


class FaultController:
    """Current fault state and the logic that applies it to a call."""

    def __init__(self) -> None:
        self._modo = "ok"
        self._delay_ms = 0
        self._fail_rate = 0.0

    # ------------------------------------------------------------- estado --
    @property
    def modo(self) -> str:
        return self._modo

    @property
    def delay_ms(self) -> int:
        return self._delay_ms

    @property
    def fail_rate(self) -> float:
        return self._fail_rate

    def estado(self) -> dict:
        """Current fault state, the payload of `GET /admin/fault`."""
        return {"mode": self._modo, "delay_ms": self._delay_ms, "fail_rate": self._fail_rate}

    def configurar(self, modo: str, delay_ms: int, fail_rate: float) -> dict:
        """Replace the current fault state."""
        modo = (modo or "ok").lower()
        if modo not in MODOS:
            raise ModoInvalido(f"modo de falla desconocido: {modo!r} (validos: {', '.join(MODOS)})")
        if delay_ms < 0:
            raise ModoInvalido("delay_ms no puede ser negativo")
        if not 0.0 <= fail_rate <= 1.0:
            raise ModoInvalido("fail_rate debe estar entre 0.0 y 1.0")

        self._modo = modo
        self._delay_ms = int(delay_ms)
        self._fail_rate = float(fail_rate)
        return self.estado()

    # ------------------------------------------------------------- fallos --
    def debe_fallar(self) -> bool:
        """Decide if this call has to fail (mode `down` or the random rate)."""
        if self._modo == "down":
            return True
        if self._fail_rate > 0.0:
            return random.random() < self._fail_rate
        return False

    async def aplicar(self) -> None:
        """Apply the current fault to one call.

        Raises `FalloPasarela` when the call must fail and sleeps when the
        gateway is slow. The payment endpoints call this before touching
        their own state, so a failed call never charges anything.
        """
        if self.debe_fallar():
            raise FalloPasarela(self._modo)

        if self._modo == "slow" and self._delay_ms > 0:
            await asyncio.sleep(self._delay_ms / 1000.0)


class FalloPasarela(Exception):
    """The simulated gateway refuses to answer the call."""

    def __init__(self, modo: str = "down") -> None:
        super().__init__(f"pasarela no disponible (modo {modo})")
        self.modo = modo
