# beaverCore

[![PyPI](https://img.shields.io/pypi/v/beavercore.svg)](https://pypi.org/project/beavercore/)
[![Python](https://img.shields.io/pypi/pyversions/beavercore.svg)](https://pypi.org/project/beavercore/)
[![License](https://img.shields.io/pypi/l/beavercore.svg)](LICENSE)

A HTTP client wrapper on top of `requests`. You bring the URL and the credentials — beaverCore handles the parts of every HTTP client you'd otherwise copy-paste: retries with exponential backoff, 429 rate-limit compliance, one-shot session-token refresh on 401, typed exceptions, connection pooling, and an observability hook.

```bash
pip install beavercore
```

## The idea

Every time someone writes a new HTTP client in Python — for an internal service, a vendor API, a third-party integration — they solve the same six problems:

1. Turning transient failures (5xx, connection resets, timeouts) into retries with backoff.
2. Respecting `Retry-After` when the upstream throttles them.
3. Refreshing an expired session token *once*, transparently, on a 401.
4. Distinguishing "auth broke" from "server sad" from "the request itself is wrong" without callers having to inspect status codes.
5. Pooling the underlying TCP connections instead of tearing them down every call.
6. Wiring the whole thing into logs/metrics without turning every method into a callback tree.

beavercore does all six once. Downstream clients become endpoint code and nothing else — the GitHub client in `example.py` is exactly that shape: a factory function plus the endpoints, no retry code, no error classification, no rate-limit code.

## Quick start

```python
from beavercore import Client, RetryPolicy

def auth(request_kwargs: dict) -> None:
    request_kwargs.setdefault("headers", {})["Authorization"] = f"Bearer {TOKEN}"

with Client(
    "https://api.example.com",
    auth=auth,
    retry=RetryPolicy(max_attempts=4),
) as client:
    things = client.get("/things").json()
```

Extension is via callables — no subclassing required. If you need to refresh a session token on a 401:

```python
def refresh() -> bool:
    global TOKEN
    TOKEN = login()
    return True   # tells beavercore to retry the original request once

Client("https://api.example.com", auth=auth, refresh=refresh)
```

`refresh` runs at most once per request. If the retry also 401s, you get `AuthError`.

## Exception hierarchy

```
HttpError                base — carries .status, .response, .attempts
├── AuthError            401/403 (extra: .refresh_attempted)
├── RateLimitError       429 after retries exhausted (extra: .retry_after)
└── TransientError       5xx or network fault, retries exhausted (extra: .last_reason)
```

Every exception carries `.response` (the raw `requests.Response`, or `None` for network faults) and `.attempts` (how many tries were made). Non-retryable non-2xx that doesn't fit one of the subclasses (e.g. 404, 418, 400) raises the base `HttpError`.

## Design decisions

The choices worth explaining because they're not obvious from the code.

### Built on `requests`, not transport-agnostic

beavercore is a *policy* layer — retries, backoff, error classification, session refresh, observability. The *transport* is [`requests`](https://requests.readthedocs.io/). A transport-agnostic version (pluggable `requests`/`httpx`) is possible but doubles the surface area for one real use case (async, via `httpx`), so it's deferred. If you need async today, use `httpx` directly — that's the correct answer.

### Compose with callables, don't subclass

The old shape of this library required subclassing `BaseClient` and overriding `_apply_auth` / `_refresh_auth`. It's gone. `Client` takes `auth` and `refresh` as callables at construction. Reasons:

- **One class, one instance per upstream.** No `GitHubClient(BaseClient)` boilerplate per API.
- **Auth is state, not behavior.** Making it a callable makes token rotation, environment-driven auth, and testing trivial.
- **Composition scales.** Wrap the callable, don't subclass the class.

### Raise, don't return

Most Python HTTP wrappers return `{"success": bool, "response": ..., "error": ...}` dicts. beavercore raises typed exceptions instead. Reasons:

- **Composes with `try/except`.** Callers can catch `AuthError` in one place instead of checking `result["success"]` on every call.
- **Doesn't collide with success payloads.** If the API you're calling returns `{"success": false, ...}` as *data*, dict-based error contracts get confusing.
- **Matches the ecosystem.** `requests.HTTPError`, `httpx.HTTPStatusError`, `openai.APIError` — every mature Python HTTP library raises.

### Session refresh runs *once*, not forever

On a 401, the `refresh` callable runs exactly once. If it returns `True`, the original request is retried once. If the retry also 401s (or `refresh` returned `False`), you get `AuthError`. Reasons:

- **Bounded blast radius.** A misconfigured refresh function can't turn a single failed call into an infinite refresh loop.
- **Refresh failures surface immediately.** If the refresh itself is broken (bad refresh token, revoked session), you see it now, not after N retries.
- **Refresh is expensive.** Most upstreams charge for refresh calls (rate-limit budget, database write, OAuth roundtrip). Don't spam it.

### Retries only on genuinely retryable failures

Retryable: connection error, timeout, 500/502/503/504, 429. Not retryable: 4xx (except 401, which becomes a refresh path). Reasons:

- **4xx is your bug.** `400 Bad Request`, `403 Forbidden`, `422 Unprocessable Entity` — retrying won't fix them. Fail fast, surface loud.
- **5xx and network faults are upstream's bug.** Retrying with jitter is the right response.
- **429 is a special case.** Technically "your fault" for exceeding a quota, but the upstream's `Retry-After` header tells you exactly how to fix it.

### Bring-your-own rate limiter

`Client` accepts a `throttle: Callable[[], None]` argument. It's called before every attempt. That's the whole contract. Reasons:

- **Different upstreams have different rules.** Some are per-second, some per-minute, some per-user, some token-bucket, some sliding-window. One shipped limiter would only fit one shape.
- **Optional by default.** Most APIs don't advertise a client-side limit — you only add one when the upstream tells you or 429s you.
- **You already have one.** Every real project has a rate-limit primitive (Redis, in-memory semaphore, third-party lib). Pass it as a callable.

### One observer, not three hooks

The client emits events to a single `observer(event: dict)` callable. Events include `request`, `response`, `retry`, `auth_refresh`, each carrying `method`, `url`, `attempt`, and event-specific fields. Reasons:

- **One signal path, not three.** Log aggregation, tracing, and metrics all speak dict.
- **Extensible.** Adding a new event kind doesn't change the API.
- **Zero cost when unused.** `observer` defaults to `None`; the fast path has one `is not None` check.

## Constructor options

| arg | default | notes |
|---|---|---|
| `base_url` | required | Trailing slash optional. |
| `auth` | `None` | `(request_kwargs) -> None`. Mutate to attach credentials. |
| `refresh` | `None` | `() -> bool`. Return `True` to retry once after a 401. |
| `retry` | `RetryPolicy()` | See below. |
| `throttle` | `None` | `() -> None`. Called before every attempt. |
| `observer` | `None` | `(event_dict) -> None`. Fires for `request`, `response`, `retry`, `auth_refresh`. |
| `timeout` | `30` | Seconds. Applied to every request. |
| `verify_ssl` | `True` | Set `False` for self-signed internal endpoints. |
| `session` | `None` | Provide a preconfigured `requests.Session` for custom adapters. |

## RetryPolicy

```python
RetryPolicy(
    max_attempts=3,    # total attempts including the first
    backoff_base=0.5,  # seconds — multiplier for 2^attempt
    backoff_cap=30.0,  # seconds — upper bound on any single sleep
    jitter=0.5,        # 0 = no jitter, 1 = full jitter of backoff_base
)
```

Immutable dataclass. `delay_for(attempt)` returns the sleep duration for a given attempt index.

## Observability

```python
def observer(event: dict) -> None:
    if event["event"] == "retry":
        logger.warning(
            "retrying %s %s (attempt %d): %s",
            event["method"], event["url"], event["attempt"] + 1, event["reason"],
        )

client = Client("https://api.example.com", auth=auth, observer=observer)
```

One hook, one signal path — enough to wire beavercore into any log aggregator, Prometheus histogram, or OpenTelemetry span.

## Worked example

`example.py` at the repo root is a small GitHub REST API client built on beavercore — bearer auth, real 429s, real 404s. It's the reference implementation for what a client on top of beavercore should look like. Try it:

```bash
export GITHUB_TOKEN=ghp_your_token
python example.py
```

## Repository layout

```
beaverCore/
├── pyproject.toml       # package + dev tools + all config
├── README.md            # this file (also the PyPI description)
├── LICENSE
├── beavercore/
│   ├── __init__.py      # public API re-exports
│   ├── client.py        # Client
│   ├── retry.py         # RetryPolicy
│   └── exceptions.py    # HttpError, AuthError, RateLimitError, TransientError
├── tests/
│   └── test_client.py
├── example.py           # runnable GitHub API demo
└── .github/workflows/
    └── publish.yml      # tag v<version> → PyPI (trusted publisher)
```

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

## Publishing

Published to PyPI via GitHub Actions using [trusted publishing](https://docs.pypi.org/trusted-publishers/). A tag matching `v<version>` triggers a release.

```bash
git tag v0.1.0
git push origin v0.1.0
```

## Sibling projects

- [beaverWeb](https://github.com/kalyanramchimmili/beaverWeb) — A micro web framework for building the services beaverCore *calls*.

## License

MIT

