"""HTTP client factory.

Every outbound HTTP call carries an explicit timeout, so a slow dependency
can never hold a request thread forever.
"""

from __future__ import annotations

import httpx


def http_client(timeout: float = 2.0, **kwargs) -> httpx.AsyncClient:
    """Async HTTP client with the same timeout for every phase.

    `timeout` applies to connect, read, write and pool, so the total wait of
    a call is bounded by it.
    """
    options: dict = {"limits": httpx.Limits(max_connections=50, max_keepalive_connections=10)}
    options.update(kwargs)
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), **options)
