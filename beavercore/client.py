"""The Client — runs the request, retries transient failures, refreshes auth."""

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

import requests

from beavercore.exceptions import AuthError, HttpError, RateLimitError, TransientError
from beavercore.retry import RetryPolicy

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

AuthHook = Callable[[dict], None]
RefreshHook = Callable[[], bool]
ThrottleHook = Callable[[], None]
ObserverHook = Callable[[dict], None]


class Client:
    """HTTP client with retry, backoff, 429 handling, one-shot auth refresh.

    :param auth: mutates ``request_kwargs`` before each attempt to attach credentials.
    :param refresh: called once on the first 401; return ``True`` to retry, ``False``
        (or omit) to raise :class:`AuthError`.
    :param throttle: called before every attempt (bring your own rate limiter).
    :param observer: called with an event dict (``event``, ``method``, ``url`` and
        event-specific fields). Events: ``request``, ``response``, ``retry``,
        ``auth_refresh``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth: AuthHook | None = None,
        refresh: RefreshHook | None = None,
        retry: RetryPolicy | None = None,
        throttle: ThrottleHook | None = None,
        observer: ObserverHook | None = None,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        session: requests.Session | None = None,
    ):
        if not base_url:
            raise ValueError("base_url is required")

        self._base_url = base_url.rstrip("/") + "/"
        self._auth = auth
        self._refresh = refresh
        self._retry = retry or RetryPolicy()
        self._throttle = throttle
        self._observer = observer
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._session = session or requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self._request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self._request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> requests.Response:
        return self._request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> requests.Response:
        return self._request("PATCH", path, **kw)

    def delete(self, path: str, **kw: Any) -> requests.Response:
        return self._request("DELETE", path, **kw)

    def _emit(self, event: str, **fields: Any) -> None:
        if self._observer is not None:
            self._observer({"event": event, **fields})

    def _sleep(self, attempt: int, delay: float) -> None:
        # Skip the sleep after the last attempt — nothing else is going to run.
        if attempt < self._retry.max_attempts - 1 and delay > 0:
            time.sleep(delay)

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = urljoin(self._base_url, path.lstrip("/"))
        kwargs.setdefault("timeout", self._timeout)
        kwargs.setdefault("verify", self._verify_ssl)

        refresh_tried = False
        last_response: requests.Response | None = None
        last_reason: str = ""

        for attempt in range(self._retry.max_attempts):
            if self._throttle is not None:
                self._throttle()

            request_kwargs = dict(kwargs)
            if self._auth is not None:
                self._auth(request_kwargs)

            self._emit("request", method=method, url=url, attempt=attempt)

            try:
                response = self._session.request(method, url, **request_kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_reason = f"network:{type(exc).__name__}"
                self._emit("retry", method=method, url=url, attempt=attempt, reason=last_reason)
                self._sleep(attempt, self._retry.delay_for(attempt))
                continue

            self._emit(
                "response", method=method, url=url, status=response.status_code, attempt=attempt
            )

            status = response.status_code

            if status == 401 and self._refresh is not None and not refresh_tried:
                refresh_tried = True
                if self._refresh():
                    self._emit("auth_refresh", method=method, url=url, attempt=attempt)
                    continue
                raise AuthError(
                    f"authentication failed for {method} {url}",
                    status=status,
                    response=response,
                    attempts=attempt + 1,
                    refresh_attempted=True,
                )

            if status in (401, 403):
                raise AuthError(
                    f"authentication failed for {method} {url} ({status})",
                    status=status,
                    response=response,
                    attempts=attempt + 1,
                    refresh_attempted=refresh_tried,
                )

            if status == 429:
                last_response = response
                last_reason = "rate_limit:429"
                retry_after = _parse_retry_after(response)
                self._emit("retry", method=method, url=url, attempt=attempt, reason=last_reason)
                delay = (
                    min(retry_after, self._retry.backoff_cap)
                    if retry_after is not None
                    else self._retry.delay_for(attempt)
                )
                self._sleep(attempt, delay)
                continue

            if status in _RETRYABLE_STATUS:
                last_response = response
                last_reason = f"5xx:{status}"
                self._emit("retry", method=method, url=url, attempt=attempt, reason=last_reason)
                self._sleep(attempt, self._retry.delay_for(attempt))
                continue

            if not response.ok:
                raise HttpError(
                    f"unexpected {status} from {method} {url}",
                    status=status,
                    response=response,
                    attempts=attempt + 1,
                )

            return response

        attempts = self._retry.max_attempts
        if last_response is not None and last_response.status_code == 429:
            raise RateLimitError(
                f"rate limit not cleared after {attempts} attempts",
                status=429,
                response=last_response,
                attempts=attempts,
                retry_after=_parse_retry_after(last_response),
            )
        raise TransientError(
            f"transient failure persisted after {attempts} attempts ({last_reason})",
            status=last_response.status_code if last_response is not None else None,
            response=last_response,
            attempts=attempts,
            last_reason=last_reason,
        )


def _parse_retry_after(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
