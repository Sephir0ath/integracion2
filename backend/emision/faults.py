"""Fault injection of the `emision` service.

The control plane of the demo scripts lives here. The state is a plain
object shared by the whole process, so `GET /admin/fault` always answers
with the mode that is currently applied to the emission.

Semantics of the modes:

* ``ok``   the service behaves normally.
* ``slow`` every emission is delayed ``delay_ms`` milliseconds, which is
  how a slow dependency is simulated (the delay is longer than the
  deadline of the caller).
* ``down`` every emission fails, which is how the last step of the saga
  is made to fail so the compensations can be shown.

``fail_rate`` is orthogonal to the mode: it is the fraction of calls that
fail randomly, between ``0.0`` and ``1.0``.
"""

from __future__ import annotations

import asyncio
import random

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MODOS = ("ok", "slow", "down")


class ModoInvalido(ValueError):
    """Raised when the requested fault mode is not one of the known ones."""


class FalloEmision(Exception):
    """The emission service refuses to issue the tickets."""

    def __init__(self, modo: str = "down") -> None:
        super().__init__(f"emision no disponible (modo {modo})")
        self.modo = modo


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

        Raises `FalloEmision` when the call must fail and sleeps when the
        service is slow. The emission applies the fault before it writes
        anything, so a failed call never leaves a ticket behind.
        """
        if self.debe_fallar():
            raise FalloEmision(self._modo)

        if self._modo == "slow" and self._delay_ms > 0:
            await asyncio.sleep(self._delay_ms / 1000.0)


class FaultBody(BaseModel):
    """Body of `POST /admin/fault`."""

    mode: str = "ok"
    delay_ms: int = 0
    fail_rate: float = 0.0


def registrar_rutas(app: FastAPI, controlador: FaultController) -> None:
    """Add the control plane of the faults to the admin application."""

    @app.post("/admin/fault")
    async def configurar_fault(cuerpo: FaultBody):
        """Inject a fault into the emission."""
        try:
            estado = controlador.configurar(cuerpo.mode, cuerpo.delay_ms, cuerpo.fail_rate)
        except ModoInvalido as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return estado

    @app.get("/admin/fault")
    async def leer_fault():
        """Read the fault that is applied right now."""
        return controlador.estado()
