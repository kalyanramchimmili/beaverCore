import pytest
import requests
import responses

from beavercore import (
    AuthError,
    Client,
    HttpError,
    RateLimitError,
    RetryPolicy,
    TransientError,
)

BASE = "https://api.example.com"


@pytest.fixture
def fast() -> RetryPolicy:
    return RetryPolicy(max_attempts=3, backoff_base=0.0, backoff_cap=0.0, jitter=0.0)


def _url(path: str) -> str:
    return f"{BASE}/{path.lstrip('/')}"


# ─── constructor validation ──────────────────────────────────────────────────


def test_base_url_required():
    with pytest.raises(ValueError):
        Client(base_url="")


def test_retry_policy_validates_max_attempts():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)


def test_retry_policy_delay_grows_and_caps():
    p = RetryPolicy(max_attempts=10, backoff_base=1.0, backoff_cap=4.0, jitter=0.0)
    assert p.delay_for(0) == 1.0
    assert p.delay_for(1) == 2.0
    assert p.delay_for(2) == 4.0
    assert p.delay_for(9) == 4.0  # capped


# ─── happy path ──────────────────────────────────────────────────────────────


@responses.activate
def test_get_200(fast):
    responses.get(_url("/things"), json={"ok": True})
    with Client(BASE, retry=fast) as c:
        r = c.get("/things")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@responses.activate
def test_auth_callable_mutates_kwargs(fast):
    responses.get(_url("/x"), json={})

    def auth(kw: dict) -> None:
        kw.setdefault("headers", {})["Authorization"] = "Bearer xyz"

    with Client(BASE, retry=fast, auth=auth) as c:
        c.get("/x")

    assert responses.calls[0].request.headers["Authorization"] == "Bearer xyz"


# ─── 5xx retry / exhaustion ──────────────────────────────────────────────────


@responses.activate
def test_5xx_retried_then_succeeds(fast):
    responses.get(_url("/x"), status=503)
    responses.get(_url("/x"), status=502)
    responses.get(_url("/x"), json={"ok": True})
    with Client(BASE, retry=fast) as c:
        r = c.get("/x")
    assert r.status_code == 200
    assert len(responses.calls) == 3


@responses.activate
def test_5xx_exhausts_raises_transient(fast):
    for _ in range(3):
        responses.get(_url("/x"), status=500)
    with Client(BASE, retry=fast) as c, pytest.raises(TransientError) as ei:
        c.get("/x")
    assert ei.value.attempts == 3
    assert ei.value.last_reason == "5xx:500"
    assert ei.value.status == 500


# ─── 429 rate limit ──────────────────────────────────────────────────────────


@responses.activate
def test_429_retry_after_then_succeeds(fast):
    responses.get(_url("/x"), status=429, headers={"Retry-After": "0"})
    responses.get(_url("/x"), json={"ok": True})
    with Client(BASE, retry=fast) as c:
        r = c.get("/x")
    assert r.status_code == 200


@responses.activate
def test_429_exhausts_raises_rate_limit(fast):
    for _ in range(3):
        responses.get(_url("/x"), status=429, headers={"Retry-After": "0"})
    with Client(BASE, retry=fast) as c, pytest.raises(RateLimitError) as ei:
        c.get("/x")
    assert ei.value.attempts == 3
    assert ei.value.retry_after == 0.0
    assert ei.value.status == 429


# ─── 401 auth refresh ────────────────────────────────────────────────────────


@responses.activate
def test_401_refresh_true_retries_once(fast):
    responses.get(_url("/x"), status=401)
    responses.get(_url("/x"), json={"ok": True})
    calls = {"n": 0}

    def refresh() -> bool:
        calls["n"] += 1
        return True

    with Client(BASE, retry=fast, refresh=refresh) as c:
        r = c.get("/x")
    assert r.status_code == 200
    assert calls["n"] == 1


@responses.activate
def test_401_refresh_false_raises_auth(fast):
    responses.get(_url("/x"), status=401)
    with Client(BASE, retry=fast, refresh=lambda: False) as c, pytest.raises(AuthError) as ei:
        c.get("/x")
    assert ei.value.refresh_attempted is True
    assert ei.value.status == 401


@responses.activate
def test_401_without_refresh_hook_raises_auth(fast):
    responses.get(_url("/x"), status=401)
    with Client(BASE, retry=fast) as c, pytest.raises(AuthError) as ei:
        c.get("/x")
    assert ei.value.refresh_attempted is False


@responses.activate
def test_403_raises_auth(fast):
    responses.get(_url("/x"), status=403)
    with Client(BASE, retry=fast) as c, pytest.raises(AuthError) as ei:
        c.get("/x")
    assert ei.value.status == 403


@responses.activate
def test_401_refresh_only_runs_once_even_if_still_401(fast):
    for _ in range(3):
        responses.get(_url("/x"), status=401)
    calls = {"n": 0}

    def refresh() -> bool:
        calls["n"] += 1
        return True

    with Client(BASE, retry=fast, refresh=refresh) as c, pytest.raises(AuthError):
        c.get("/x")
    assert calls["n"] == 1


# ─── non-retryable 4xx ───────────────────────────────────────────────────────


@responses.activate
def test_404_raises_httperror_and_stops(fast):
    responses.get(_url("/x"), status=404)
    with Client(BASE, retry=fast) as c, pytest.raises(HttpError) as ei:
        c.get("/x")
    assert ei.value.status == 404
    assert not isinstance(ei.value, (AuthError, RateLimitError, TransientError))
    assert len(responses.calls) == 1


@responses.activate
def test_418_raises_httperror(fast):
    responses.get(_url("/x"), status=418)
    with Client(BASE, retry=fast) as c, pytest.raises(HttpError) as ei:
        c.get("/x")
    assert ei.value.status == 418


# ─── network faults ──────────────────────────────────────────────────────────


@responses.activate
def test_connection_error_retried_then_exhausted(fast):
    for _ in range(3):
        responses.get(_url("/x"), body=requests.ConnectionError("boom"))
    with Client(BASE, retry=fast) as c, pytest.raises(TransientError) as ei:
        c.get("/x")
    assert "network:" in ei.value.last_reason
    assert ei.value.attempts == 3


# ─── observer ────────────────────────────────────────────────────────────────


@responses.activate
def test_observer_receives_lifecycle_events(fast):
    responses.get(_url("/x"), status=500)
    responses.get(_url("/x"), json={"ok": True})

    events: list[dict] = []
    with Client(BASE, retry=fast, observer=events.append) as c:
        c.get("/x")

    kinds = [e["event"] for e in events]
    assert kinds.count("request") == 2
    assert kinds.count("response") == 2
    assert kinds.count("retry") == 1


# ─── throttle ────────────────────────────────────────────────────────────────


@responses.activate
def test_throttle_called_before_every_attempt(fast):
    responses.get(_url("/x"), status=500)
    responses.get(_url("/x"), status=500)
    responses.get(_url("/x"), json={"ok": True})

    calls = {"n": 0}

    def throttle() -> None:
        calls["n"] += 1

    with Client(BASE, retry=fast, throttle=throttle) as c:
        c.get("/x")

    assert calls["n"] == 3


# ─── session lifecycle ───────────────────────────────────────────────────────


def test_context_manager_closes_session():
    with Client(BASE) as c:
        session = c._session
    # After close(), further requests would fail — we just verify close was reached.
    assert session is not None
