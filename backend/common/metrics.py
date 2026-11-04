"""Prometheus metrics.

There is one registry per process and one singleton per metric name, so
several modules can reference the same metric without the duplicated
timeseries error of prometheus_client.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

_METRICS: dict[str, object] = {}


def counter(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Counter:
    """Return the process wide counter named `name`."""
    key = f"counter:{name}"
    if key not in _METRICS:
        _METRICS[key] = Counter(name, documentation, tuple(labelnames), registry=REGISTRY)
    return _METRICS[key]  # type: ignore[return-value]


def gauge(name: str, documentation: str, labelnames: Sequence[str] = ()) -> Gauge:
    """Return the process wide gauge named `name`."""
    key = f"gauge:{name}"
    if key not in _METRICS:
        _METRICS[key] = Gauge(name, documentation, tuple(labelnames), registry=REGISTRY)
    return _METRICS[key]  # type: ignore[return-value]


def histogram(
    name: str,
    documentation: str,
    labelnames: Sequence[str] = (),
    buckets: Iterable[float] | None = None,
) -> Histogram:
    """Return the process wide histogram named `name`."""
    key = f"histogram:{name}"
    if key not in _METRICS:
        options = {"buckets": list(buckets)} if buckets is not None else {}
        _METRICS[key] = Histogram(name, documentation, tuple(labelnames), registry=REGISTRY, **options)
    return _METRICS[key]  # type: ignore[return-value]
