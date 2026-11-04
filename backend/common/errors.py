"""Domain errors shared by the services.

Every error carries a stable machine readable code, so the gateway, the BFF
and the saga orchestrator can react to it without parsing free text.
"""

from __future__ import annotations


class CODES:
    """Stable error codes."""

    ALREADY_EXISTS = "ALREADY_EXISTS"
    NOT_FOUND = "NOT_FOUND"
    INVALID = "INVALID"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    BULKHEAD_SATURATED = "BULKHEAD_SATURATED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    CONFLICT_IDEMPOTENCIA = "CONFLICT_IDEMPOTENCIA"


HTTP_STATUS_BY_CODE: dict[str, int] = {
    CODES.ALREADY_EXISTS: 409,
    CODES.NOT_FOUND: 404,
    CODES.INVALID: 400,
    CODES.PAYMENT_REQUIRED: 402,
    CODES.DEPENDENCY_UNAVAILABLE: 503,
    CODES.BULKHEAD_SATURATED: 503,
    CODES.CIRCUIT_OPEN: 503,
    CODES.CONFLICT_IDEMPOTENCIA: 409,
}


class ServiceError(Exception):
    """Error with a stable code and an HTTP status."""

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status if http_status is not None else HTTP_STATUS_BY_CODE.get(code, 400)
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def status_for(code: str) -> int:
    """HTTP status associated with an error code."""
    return HTTP_STATUS_BY_CODE.get(code, 400)
