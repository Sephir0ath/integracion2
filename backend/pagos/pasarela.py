"""Resilient client of the external payment gateway.

The `pasarela` is the fragile dependency of the domain: it can be slow, it
can be down, and it knows nothing about our saga. This module is the only
place in the service that talks to it, and every call goes through the same
chain of protection:

    bulkhead  ->  circuit breaker  ->  retry with backoff  ->  HTTP call

* The **bulkhead** is the outermost gate. At most `BULKHEAD_MAX_CONCURRENT`
  calls run at the same time and at most `BULKHEAD_MAX_PENDING` wait in the
  queue; the excess is rejected immediately, so a slow gateway can never
  exhaust this process. The rejected call never reaches the network and it
  is not counted as a failure of the dependency: our own load shedding says
  nothing about the health of the gateway.
* The **circuit breaker** is inside the bulkhead, so its statistics
  describe the health of the gateway and not our own load shedding. While
  it is OPEN the call fails fast, without touching the network, and the
  service degrades gracefully with a stable error code.
* The **retry with exponential backoff** is inside the breaker, so an open
  circuit never retries against a dead dependency. Only transient failures
  (timeout, error of transport, answer 5xx) are retried.
* Every attempt carries the timeout of `PASARELA_TIMEOUT` (connect and
  read) through `common.http_client.http_client`.
"""

from __future__ import annotations

import logging
import time

import httpx

from common.errors import CODES, ServiceError
from common.http_client import http_client
from common.logging import log_event
from common.metrics import counter, histogram
from common.resilience.bulkhead import Bulkhead, BulkheadRejected
from common.resilience.circuit_breaker import CircuitBreaker, CircuitOpenError
from common.resilience.retry import with_retry

logger = logging.getLogger("pagos.pasarela")

# Degradation messages reported by `EstadoCircuito`.
DEGRADADO_PASARELA = "degradado: pasarela no disponible"
DEGRADADO_BULKHEAD = "degradado: pasarela saturada (bulkhead)"

_solicitudes = counter(
    "pasarela_requests_total",
    "Llamadas http hechas a la pasarela externa",
    ["result"],
)
_latencia = histogram(
    "pasarela_request_duration_seconds",
    "Duración de las llamadas http a la pasarela externa",
)


class FalloTransitorio(Exception):
    """A failure of the gateway that is worth retrying.

    It carries the `ServiceError` that the caller finally sees, so the
    retry helper only repeats transient failures and the error code of the
    domain is preserved.
    """

    def __init__(self, error: ServiceError) -> None:
        super().__init__(error.message)
        self.error = error


class PasarelaClient:
    """HTTP client of the payment gateway, hardened with the shared patterns."""

    def __init__(
        self,
        settings,
        client: httpx.AsyncClient | None = None,
        *,
        retry_base_delay: float = 0.1,
        retry_max_delay: float = 1.0,
    ) -> None:
        self._settings = settings
        self._base_url = settings.pasarela_url.rstrip("/")
        self._owns_client = client is None
        self._client = client if client is not None else http_client(timeout=settings.pasarela_timeout)
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._degradacion = ""

        self.breaker = CircuitBreaker(
            "pasarela",
            failure_threshold=settings.circuit_failure_threshold,
            reset_timeout=settings.circuit_reset_timeout,
            half_open_max_calls=settings.circuit_half_open_max_calls,
        )
        self.bulkhead = Bulkhead(
            "pasarela",
            max_concurrent=settings.bulkhead_max_concurrent,
            max_pending=settings.bulkhead_max_pending,
        )

    # --------------------------------------------------------------- api --
    async def cobrar(self, monto_centavos: int, idempotency_key: str) -> dict:
        """Ask the gateway for a charge. The key makes the charge unique."""
        cuerpo = {"monto_centavos": int(monto_centavos), "idempotency_key": idempotency_key}
        return await self._llamar("POST", "/pagos", cuerpo)

    async def reembolsar(self, pago_id: str) -> dict:
        """Ask the gateway for the refund of a payment."""
        return await self._llamar("POST", f"/pagos/{pago_id}/reembolso", None)

    async def close(self) -> None:
        """Close the underlying HTTP client when this instance owns it."""
        if self._owns_client:
            await self._client.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        """The underlying HTTP client, where the timeout is configured."""
        return self._client

    # --------------------------------------------------------- proteccion --
    async def _llamar(self, metodo: str, ruta: str, cuerpo: dict | None) -> dict:
        """Run one guarded call to the gateway.

        The bulkhead is the outermost gate (our own load shedding must not
        be counted as a failure of the dependency), the circuit breaker
        comes next (it knows the health of the dependency) and the retry
        with backoff lives inside the breaker, so an open circuit never
        retries against a dead dependency.
        """
        async def _intento():
            return await self._http(metodo, ruta, cuerpo)

        async def _con_reintentos():
            # El circuito va **dentro** del reintento y no al reves: asi cada
            # intento fallido cuenta como fallo y el circuito abre a tiempo.
            # Con el circuito por fuera, el breaker solo veia el fallo al
            # final de la secuencia de reintentos: con la dependencia lenta
            # (que es justo lo que provoca la falla en cascada) nunca
            # llegaba al umbral y cada llamada se comia el timeout entero.
            # Un circuito abierto corta el reintento: `CircuitOpenError` no
            # es transitorio, asi que no se reintenta contra una dependencia
            # que ya se sabe mala.
            return await with_retry(
                lambda: self.breaker.call(_intento),
                attempts=self._settings.pasarela_max_retries + 1,
                base_delay=self._retry_base_delay,
                max_delay=self._retry_max_delay,
                retry_on=(FalloTransitorio,),
                operation="pasarela",
            )

        try:
            resultado = await self.bulkhead.run(_con_reintentos)
        except BulkheadRejected as exc:
            # Our own load shedding: it is not a failure of the dependency,
            # so the circuit must not count it.
            self._degradacion = DEGRADADO_BULKHEAD
            log_event(logger, "pasarela_bulkhead_rechazada", ruta=ruta, detalle=str(exc))
            raise ServiceError(
                CODES.BULKHEAD_SATURATED,
                "degradado: la pasarela esta saturada, intente de nuevo en un momento",
                details={"motivo": str(exc), "degradacion": self._degradacion},
            ) from exc
        except CircuitOpenError as exc:
            self._degradacion = DEGRADADO_PASARELA
            log_event(logger, "pasarela_circuito_abierto", ruta=ruta)
            raise ServiceError(
                CODES.CIRCUIT_OPEN,
                "degradado: pasarela no disponible (circuito abierto)",
                details={"motivo": str(exc), "degradacion": self._degradacion},
            ) from exc
        except FalloTransitorio as exc:
            self._degradacion = DEGRADADO_PASARELA
            log_event(
                logger,
                "pasarela_error",
                ruta=ruta,
                codigo=exc.error.code,
                mensaje=exc.error.message,
            )
            raise exc.error from exc
        except ServiceError as exc:
            # The gateway answered and rejected the call: the call failed, so
            # the circuit counts it, but it is not transient and the retry
            # helper does not repeat it.
            log_event(logger, "pasarela_rechazo", ruta=ruta, codigo=exc.code, mensaje=exc.message)
            raise

        self._degradacion = ""
        return resultado

    async def _http(self, metodo: str, ruta: str, cuerpo: dict | None) -> dict:
        """One HTTP attempt against the gateway, with the configured timeout."""
        url = f"{self._base_url}{ruta}"
        inicio = time.perf_counter()
        try:
            respuesta = await self._client.request(metodo, url, json=cuerpo)
        except httpx.TimeoutException as exc:
            _solicitudes.labels("timeout").inc()
            _latencia.observe(time.perf_counter() - inicio)
            raise FalloTransitorio(
                ServiceError(
                    CODES.DEPENDENCY_UNAVAILABLE,
                    f"la pasarela no respondio a tiempo ({self._settings.pasarela_timeout}s)",
                    details={"timeout": self._settings.pasarela_timeout, "ruta": ruta},
                )
            ) from exc
        except httpx.HTTPError as exc:
            _solicitudes.labels("error").inc()
            _latencia.observe(time.perf_counter() - inicio)
            raise FalloTransitorio(
                ServiceError(
                    CODES.DEPENDENCY_UNAVAILABLE,
                    f"la pasarela no esta disponible: {exc}",
                    details={"ruta": ruta},
                )
            ) from exc

        _latencia.observe(time.perf_counter() - inicio)

        if respuesta.status_code >= 500:
            _solicitudes.labels("error").inc()
            raise FalloTransitorio(
                ServiceError(
                    CODES.DEPENDENCY_UNAVAILABLE,
                    f"la pasarela respondio {respuesta.status_code}",
                    details={"status": respuesta.status_code, "ruta": ruta},
                )
            )

        if respuesta.status_code >= 400:
            _solicitudes.labels("rechazo").inc()
            raise ServiceError(
                CODES.INVALID,
                f"la pasarela rechazo el cobro: {respuesta.status_code}",
                details={"status": respuesta.status_code, "ruta": ruta},
            )

        _solicitudes.labels("ok").inc()
        return respuesta.json()

    # ------------------------------------------------------- observabilidad --
    def degradacion(self) -> str:
        """Degradation currently applied, empty when the dependency is healthy."""
        if self.breaker.state == "OPEN":
            return DEGRADADO_PASARELA
        return self._degradacion

    def snapshot(self) -> dict:
        """Live state of the protection chain, used by `EstadoCircuito`."""
        circuito = self.breaker.snapshot()
        recursos = self.bulkhead.snapshot()
        return {
            "estado": circuito["estado"],
            "fallos_consecutivos": circuito["fallos_consecutivos"],
            "llamadas_rechazadas": circuito["llamadas_rechazadas"],
            "llamadas_en_curso": recursos["admitidos"],
            "rechazos_bulkhead": recursos["rechazos"],
            "fallback": self.degradacion(),
        }
