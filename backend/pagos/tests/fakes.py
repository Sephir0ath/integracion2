"""Fakes used by the tests of the `pagos` service.

The tests must run without PostgreSQL and without a running pasarela, so
the database session and the gateway client are replaced by fakes that
behave like the real ones:

* `FakeSession` keeps the committed state apart from the pending one, so a
  rollback really discards what the transaction wrote.
* `FakeSessionProvider` mimics `common.db.session_scope`: it commits when
  the block ends without an error and rolls back when it ends with one.
* `FakePasarela` replaces the resilient client of the gateway.
* `FakeTransport` replaces the network of `httpx`.
"""

from __future__ import annotations

import httpx


class FakeSession:
    """Transactional fake of a SQLAlchemy session."""

    def __init__(self) -> None:
        self._committed: dict[tuple[str, str], object] = {}
        self._pending: dict[tuple[str, str], object] = {}
        self._snapshots: dict[tuple[str, str], dict] = {}
        self.commits = 0
        self.rollbacks = 0

    # ------------------------------------------------------------ interno --
    @staticmethod
    def _clave(obj) -> tuple[str, str]:
        tabla = obj.__mapper__.local_table.name
        atributo = obj.__mapper__.primary_key[0].key
        return tabla, str(getattr(obj, atributo))

    @staticmethod
    def _estado(obj) -> dict:
        return {atributo.key: getattr(obj, atributo.key) for atributo in obj.__mapper__.column_attrs}

    # ------------------------------------------------------------- session --
    def get(self, model, pk):
        """Read one row by primary key."""
        clave = (model.__tablename__, str(pk))
        if clave in self._pending:
            return self._pending[clave]
        obj = self._committed.get(clave)
        if obj is None:
            return None
        # The row enters the transaction: a rollback restores it.
        self._pending[clave] = obj
        self._snapshots[clave] = self._estado(obj)
        return obj

    def add(self, obj):
        """Insert one row in the transaction."""
        self._pending[self._clave(obj)] = obj

    def flush(self):
        """Nothing to do: the fake commits at the end of the transaction."""

    def commit(self):
        """Make the pending state durable."""
        self._committed.update(self._pending)
        self._pending.clear()
        self._snapshots.clear()
        self.commits += 1

    def rollback(self):
        """Discard the pending state."""
        for clave, obj in self._pending.items():
            estado = self._snapshots.get(clave)
            if estado is not None:
                for atributo, valor in estado.items():
                    setattr(obj, atributo, valor)
            else:
                self._committed.pop(clave, None)
        self._pending.clear()
        self._snapshots.clear()
        self.rollbacks += 1

    def close(self):
        """Nothing to close."""

    # --------------------------------------------------------- inspeccion --
    def filas(self, model) -> list:
        """Rows of one model, as they are committed."""
        tabla = model.__tablename__
        return [obj for (nombre, _), obj in self._committed.items() if nombre == tabla]

    def filas_pendientes(self, model) -> list:
        """Rows of one model that the open transaction wrote."""
        tabla = model.__tablename__
        return [obj for (nombre, _), obj in self._pending.items() if nombre == tabla]


class FakeSessionProvider:
    """Context manager factory that mimics `common.db.session_scope`."""

    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> "FakeSessionProvider":
        return self

    def __enter__(self) -> FakeSession:
        return self.session

    def __exit__(self, tipo, valor, traza) -> bool:
        if tipo is None:
            self.session.commit()
        else:
            self.session.rollback()
        return False


class FakePasarela:
    """Stub of the resilient client of the gateway."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple] = []

    async def cobrar(self, monto_centavos: int, idempotency_key: str) -> dict:
        self.calls.append(("cobrar", int(monto_centavos), idempotency_key))
        if self.error is not None:
            raise self.error
        return {
            "pago_id": "pgw_falso",
            "estado": "CONFIRMADO",
            "referencia": "REF-FALSO",
        }

    async def reembolsar(self, pago_id: str) -> dict:
        self.calls.append(("reembolsar", pago_id))
        if self.error is not None:
            raise self.error
        return {"estado": "REEMBOLSADO", "pago_id": pago_id}

    def snapshot(self) -> dict:
        return {
            "estado": "CLOSED",
            "fallos_consecutivos": 0,
            "llamadas_rechazadas": 0,
            "llamadas_en_curso": 0,
            "rechazos_bulkhead": 0,
            "fallback": "",
        }


class FakeTransport(httpx.AsyncBaseTransport):
    """HTTP transport that answers with the handler of the test."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.llamadas: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.llamadas.append(request)
        return await self.handler(request)
