"""Shared API and business errors."""

from __future__ import annotations


class ModerationError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(ModerationError):
    code = "VALIDATION_ERROR"


class BusinessError(ModerationError):
    code = "BUSINESS_ERROR"


class UnauthorizedError(ModerationError):
    status_code = 401
    code = "UNAUTHORIZED"


class ForbiddenError(ModerationError):
    status_code = 403
    code = "FORBIDDEN"


class NotFoundError(ModerationError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(ModerationError):
    status_code = 409
    code = "CONFLICT"


class UpstreamError(ModerationError):
    status_code = 500
    code = "UPSTREAM_ERROR"
