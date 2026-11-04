"""Shared library for the ticketera microservices.

Every service in the system imports from this package: configuration,
structured logging, Prometheus metrics, SQLAlchemy session handling, the
idempotency store, the gRPC server/client helpers and the resilience
patterns (Circuit Breaker, Bulkhead, retry with backoff).

The resilience patterns live here on purpose. There must be exactly one
implementation of each pattern, reused by every service, so the behaviour
observed in the demo is the same behaviour everywhere.
"""
