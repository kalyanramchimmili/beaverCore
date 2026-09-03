"""Typed exception hierarchy raised by :class:`beavercore.Client`.

Every exception carries the raw :class:`requests.Response` (when a response was
received), the HTTP status, and the total attempts made. Subclasses add fields
specific to why they were raised — ``retry_after`` on 429, ``last_reason`` on
transient failures, ``refresh_attempted`` on 401/403.
"""

from typing import Any

import requests


class HttpError(Exception):
    """Base for every error raised by :class:`Client`."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        response: requests.Response | None = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.status = status
        self.response = response
        self.attempts = attempts

    @property
    def status_code(self) -> int | None:
        return self.status


class AuthError(HttpError):
    """401/403. ``refresh_attempted`` records whether the refresh hook ran."""

    def __init__(self, message: str, *, refresh_attempted: bool = False, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.refresh_attempted = refresh_attempted


class RateLimitError(HttpError):
    """429 that outlasted the retry budget. ``retry_after`` from the last response."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any):
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class TransientError(HttpError):
    """5xx or network fault that outlasted the retry budget.

    ``last_reason`` is a short tag like ``"5xx:503"`` or ``"network:Timeout"``.
    """

    def __init__(self, message: str, *, last_reason: str = "", **kwargs: Any):
        super().__init__(message, **kwargs)
        self.last_reason = last_reason
