"""Structured JSON logging.

One JSON object per line on stdout. The demo scripts and the report read
those lines directly, so no log aggregator is needed to show the evidence.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": os.getenv("SERVICE_NAME", "servicio"),
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or _request_id.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in (getattr(record, "event_fields", None) or {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(service_name: str) -> None:
    """Install the JSON formatter on the root logger."""
    os.environ.setdefault("SERVICE_NAME", service_name)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    """Log a structured event with extra fields.

    `event` is the stable name of the event (for example `saga_compensated`);
    `fields` carries the context of that occurrence.
    """
    logger.info(event, extra={"event_fields": fields})


def set_request_id(value: str | None) -> None:
    """Bind a correlation id to the current context."""
    _request_id.set(value)


def get_request_id() -> str | None:
    """Return the correlation id bound to the current context."""
    return _request_id.get()
