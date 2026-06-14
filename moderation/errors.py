"""Shared API and business errors."""

from __future__ import annotations


class ModerationError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(ModerationError):
    pass


class BusinessError(ModerationError):
    pass


class UnauthorizedError(ModerationError):
    status_code = 401


class ForbiddenError(ModerationError):
    status_code = 403


class NotFoundError(ModerationError):
    status_code = 404


class ConflictError(ModerationError):
    status_code = 409


class UpstreamError(ModerationError):
    status_code = 500

