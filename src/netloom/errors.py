"""Domain errors. Each one knows the HTTP status it maps to."""

from __future__ import annotations


class NetloomError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_body(self) -> dict:
        body: dict = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return body


class ValidationFailed(NetloomError):
    status = 422
    code = "validation_failed"


class BadRequest(NetloomError):
    status = 400
    code = "bad_request"


class NotFound(NetloomError):
    status = 404
    code = "not_found"


class Conflict(NetloomError):
    status = 409
    code = "conflict"


class MethodNotAllowed(NetloomError):
    status = 405
    code = "method_not_allowed"
