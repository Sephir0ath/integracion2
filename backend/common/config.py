"""Environment driven configuration.

Every service reads its configuration from environment variables, so the same
image runs unchanged in every container. Values are read once and cached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _str(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Resolved configuration of a service."""

    service_name: str = "servicio"
    log_level: str = "INFO"

    # gRPC server and admin HTTP port (health + metrics).
    grpc_port: int = 50051
    admin_port: int = 9001
    grpc_deadline: float = 5.0

    # Persistence.
    database_url: str | None = None
    redis_url: str | None = None

    # External payment gateway (the fragile dependency).
    pasarela_url: str = "http://pasarela:9500"
    pasarela_timeout: float = 2.0
    pasarela_max_retries: int = 2

    # Resilience patterns.
    circuit_failure_threshold: int = 3
    circuit_reset_timeout: float = 8.0
    circuit_half_open_max_calls: int = 1
    bulkhead_max_concurrent: int = 5
    bulkhead_max_pending: int = 10

    # Gateway authentication and rate limiting.
    jwt_secret: str = "ticketera-dev-secret"
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 3600
    rate_limit_per_minute: int = 60
    rate_limit_burst: int = 20

    # Internal service targets.
    butacas_target: str = "butacas:50051"
    pagos_target: str = "pagos:50052"
    emision_target: str = "emision:50053"
    compras_target: str = "compras:50054"
    bff_url: str = "http://bff:8081"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=_str("SERVICE_NAME", "servicio"),
            log_level=_str("LOG_LEVEL", "INFO"),
            grpc_port=_int("GRPC_PORT", 50051),
            admin_port=_int("ADMIN_PORT", 9001),
            grpc_deadline=_float("GRPC_DEADLINE", 5.0),
            database_url=_str("DATABASE_URL"),
            redis_url=_str("REDIS_URL"),
            pasarela_url=_str("PASARELA_URL", "http://pasarela:9500"),
            pasarela_timeout=_float("PASARELA_TIMEOUT", 2.0),
            pasarela_max_retries=_int("PASARELA_MAX_RETRIES", 2),
            circuit_failure_threshold=_int("CIRCUIT_FAILURE_THRESHOLD", 3),
            circuit_reset_timeout=_float("CIRCUIT_RESET_TIMEOUT", 8.0),
            circuit_half_open_max_calls=_int("CIRCUIT_HALF_OPEN_MAX_CALLS", 1),
            bulkhead_max_concurrent=_int("BULKHEAD_MAX_CONCURRENT", 5),
            bulkhead_max_pending=_int("BULKHEAD_MAX_PENDING", 10),
            jwt_secret=_str("JWT_SECRET", "ticketera-dev-secret"),
            jwt_algorithm=_str("JWT_ALGORITHM", "HS256"),
            jwt_ttl_seconds=_int("JWT_TTL_SECONDS", 3600),
            rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 60),
            rate_limit_burst=_int("RATE_LIMIT_BURST", 20),
            butacas_target=_str("BUTACAS_TARGET", "butacas:50051"),
            pagos_target=_str("PAGOS_TARGET", "pagos:50052"),
            emision_target=_str("EMISION_TARGET", "emision:50053"),
            compras_target=_str("COMPRAS_TARGET", "compras:50054"),
            bff_url=_str("BFF_URL", "http://bff:8081"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached configuration of the running service."""
    return Settings.from_env()
