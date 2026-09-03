"""beavercore — a small HTTP client wrapper.

One class (:class:`Client`) that runs the request, retries transient failures,
respects ``Retry-After``, refreshes stale auth once, and raises typed errors.
Compose with callables — no subclassing required.
"""

from beavercore.client import Client
from beavercore.exceptions import (
    AuthError,
    HttpError,
    RateLimitError,
    TransientError,
)
from beavercore.retry import RetryPolicy

__all__ = [
    "AuthError",
    "Client",
    "HttpError",
    "RateLimitError",
    "RetryPolicy",
    "TransientError",
]
__version__ = "0.1.0"
