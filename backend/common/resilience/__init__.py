"""Resilience patterns shared by every service.

* `circuit_breaker` isolates a dependency that is failing.
* `bulkhead` isolates the resources spent on that dependency.
* `retry` retries a transient failure with exponential backoff.
"""

from common.resilience.bulkhead import Bulkhead, BulkheadRejected
from common.resilience.circuit_breaker import (
    CLOSED,
    HALF_OPEN,
    OPEN,
    CircuitBreaker,
    CircuitOpenError,
)
from common.resilience.retry import with_retry

__all__ = [
    "Bulkhead",
    "BulkheadRejected",
    "CircuitBreaker",
    "CircuitOpenError",
    "CLOSED",
    "HALF_OPEN",
    "OPEN",
    "with_retry",
]
