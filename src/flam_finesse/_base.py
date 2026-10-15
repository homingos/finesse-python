"""HTTP plumbing shared by the sync and async clients: auth, retries, error mapping."""

from __future__ import annotations

import asyncio
import copy
import email.utils
import os
import platform
import random
import time
import uuid
from typing import Any, Callable, Dict, Mapping, Optional, Union

import httpx

from ._errors import APIConnectionError, APITimeoutError, FinesseError, make_status_error
from ._logging import logger
from ._version import __version__

DEFAULT_BASE_URL = "https://fi.production.flamapis.com"
API_PREFIX = "/flam-voice-svc/api/v1/public"
# Synthesis on a cold GPU can take tens of seconds before the first byte.
DEFAULT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
DEFAULT_MAX_RETRIES = 2
CLIENT_NAME = f"flam-finesse-python/{__version__}"

# GET/PATCH/DELETE are safe to repeat: retry timeouts, 429 (the engine's rate limit) and 5xx.
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
# POSTs create voices and bill synthesis. Only retry when the server certainly did nothing:
# 429, 502 (engine unavailable or warming up; failed synthesis is refunded) and 503, or when
# the connection never opened. A timeout after the request was sent is not retried, because
# the server may have finished (and billed) it.
_POST_RETRY_STATUSES = frozenset({429, 502, 503})
_UNSENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# A short-lived token is spent just before the handler runs, after every check that can refuse
# the request. Only a 429 comes before the spend; a retry after a 5xx or a dropped connection
# would fail with 401 token_invalid and hide the real error.
_TOKEN_POLICY = "TOKEN"
_MAX_BACKOFF = 8.0

TimeoutTypes = Union[float, httpx.Timeout, None]
"""Seconds or an ``httpx.Timeout``. ``None`` means the default: 120 s (10 s to connect), or the
timeout of an ``http_client`` you pass. Use ``httpx.Timeout(None)`` to disable timeouts."""


class _Config:
    def __init__(
        self,
        *,
        api_key: Optional[str],
        base_url: Optional[str],
        timeout: TimeoutTypes,
        max_retries: int,
        default_headers: Optional[Mapping[str, str]],
        access_token: Optional[str] = None,
    ) -> None:
        if api_key and access_token:
            raise FinesseError("Pass api_key or access_token, not both.")
        if not access_token:
            api_key = api_key if api_key is not None else os.environ.get("FINESSE_API_KEY")
            if not api_key:
                raise FinesseError(
                    "No API key. Pass api_key=... or set the FINESSE_API_KEY environment "
                    "variable (or pass access_token=... for a short-lived token)."
                )
        self.api_key: Optional[str] = api_key or None
        self.access_token: Optional[str] = access_token or None
        base = base_url or os.environ.get("FINESSE_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.server = _ServerState()
        self.headers: Dict[str, str] = {
            "User-Agent": f"{CLIENT_NAME} python/{platform.python_version()}",
            "X-Finesse-Client": CLIENT_NAME,
            "Accept": "application/json",
        }
        if self.access_token:
            # A short-lived token (tokens.create) is a Bearer credential, good for one
            # speech.create / speech.stream call or one realtime session.
            self.headers["Authorization"] = f"Bearer {self.access_token}"
        else:
            self.headers["X-API-Key"] = self.api_key or ""
        if default_headers:
            self.headers.update(default_headers)

    @property
    def request_timeout(self) -> Any:
        """Per-request timeout: the one set on this SDK client, else the HTTP client's."""
        return self.timeout if self.timeout is not None else httpx.USE_CLIENT_DEFAULT

    def url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    @property
    def ws_base_url(self) -> str:
        if self.base_url.startswith("https://"):
            return "wss://" + self.base_url[len("https://") :]
        if self.base_url.startswith("http://"):
            return "ws://" + self.base_url[len("http://") :]
        return self.base_url


def unwrap(response: httpx.Response) -> Any:
    """Strip the ``{"status", "error", "data"}`` envelope from a JSON success body."""
    body = response.json()
    if isinstance(body, dict) and "data" in body and body.get("error") is False:
        return body["data"]
    return body


class _ServerState:
    """What this client has learned about the server, shared by its ``with_options`` copies."""

    def __init__(self) -> None:
        self.honours_idempotency_keys = False


def _idempotency(
    config: _Config, method: str, idempotent: bool
) -> tuple[Mapping[str, str], Optional[str]]:
    """Headers for a request, and its Idempotency-Key (``None`` when it has none).

    An idempotent call carries one key for all of its attempts (yours, if you passed one in
    ``extra_headers``), so a server that honours keys runs it at most once.
    """
    if not idempotent or method != "POST":
        return config.headers, None
    for k, v in config.headers.items():
        if k.lower() == "idempotency-key":
            return config.headers, v
    key = str(uuid.uuid4())
    return {**config.headers, "Idempotency-Key": key}, key


def _retry_policy(
    config: _Config, method: str, key: Optional[str], response: Optional[httpx.Response]
) -> str:
    """The method whose retry rules apply. A keyed POST is retried like a read, but only once
    the server has shown it honours keys by echoing one back; until then (an older server)
    it keeps the cautious POST rules, so nothing can run twice. A request authenticated by a
    short-lived token follows the token rules whatever its method."""
    if key is not None and response is not None and response.headers.get("Idempotency-Key") == key:
        config.server.honours_idempotency_keys = True
    if config.access_token:
        return _TOKEN_POLICY
    if key is None:
        return method
    return "GET" if config.server.honours_idempotency_keys else method


def _in_progress(response: httpx.Response) -> bool:
    if response.status_code != 409:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("code") == "idempotency_in_progress"


def _wait_budget(config: _Config) -> float:
    t = config.timeout
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, httpx.Timeout) and t.read is not None:
        return float(t.read)
    return DEFAULT_TIMEOUT.read or 120.0


def _should_retry_error(method: str, exc: Exception) -> bool:
    return method not in ("POST", _TOKEN_POLICY) or isinstance(exc, _UNSENT_ERRORS)


def _should_retry_status(method: str, status: int) -> bool:
    if method == _TOKEN_POLICY:
        return status == 429
    return status in (_POST_RETRY_STATUSES if method == "POST" else _RETRY_STATUSES)


def _transport_error(exc: httpx.TransportError, request: httpx.Request) -> APIConnectionError:
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError(request=request)
    return APIConnectionError(str(exc) or "Connection error.", request=request)


def _log_response(
    request: httpx.Request, response: httpx.Response, started: float, attempt: int
) -> None:
    if logger.isEnabledFor(10):  # DEBUG
        logger.debug(
            "%s %s -> %s in %.0f ms%s (request id %s)",
            request.method,
            request.url.copy_with(query=None),
            response.status_code,
            (time.monotonic() - started) * 1000,
            f", attempt {attempt + 1}" if attempt else "",
            response.headers.get("X-Request-Id", "-"),
        )


def _log_retry(request: httpx.Request, delay: float, attempt: int, reason: str) -> None:
    logger.info(
        "retrying %s %s in %.2f s (attempt %d failed: %s)",
        request.method,
        request.url.copy_with(query=None),
        delay,
        attempt + 1,
        reason,
    )


def _retry_delay(attempt: int, response: Optional[httpx.Response]) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                seconds = float(retry_after)
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    seconds = parsed.timestamp() - time.time()
                except (TypeError, ValueError):
                    seconds = -1.0
            if 0 <= seconds <= 60:
                return seconds
    base = min(0.5 * (2.0**attempt), _MAX_BACKOFF)
    return base * (1 - 0.25 * random.random())


class SyncHTTP:
    def __init__(self, config: _Config, http_client: Optional[httpx.Client]) -> None:
        self.config = config
        self._owns_client = http_client is None
        self.client = http_client or httpx.Client(timeout=config.timeout or DEFAULT_TIMEOUT)
        self.on_response: Optional[Callable[[httpx.Response], None]] = None

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Any = None,
        stream: bool = False,
        idempotent: bool = False,
    ) -> httpx.Response:
        """Send a request, retrying transient failures, and raise on an error status.

        With ``stream=True`` the caller must close the returned response.
        """
        headers, key = _idempotency(self.config, method, idempotent)
        attempt = 0
        in_progress_until: Optional[float] = None
        while True:
            request = self.client.build_request(
                method,
                self.config.url(path),
                json=json,
                params=params,
                data=data,
                files=files,
                headers=headers,
                timeout=self.config.request_timeout,
            )
            try:
                started = time.monotonic()
                response = self.client.send(request, stream=stream)
                _log_response(request, response, started, attempt)
                _retry_policy(self.config, method, key, response)  # learn whether keys are honoured
                if response.status_code >= 400 and stream:
                    try:
                        response.read()
                    finally:
                        response.close()
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries and _should_retry_error(
                    _retry_policy(self.config, method, key, None), exc
                ):
                    delay = _retry_delay(attempt, None)
                    _log_retry(request, delay, attempt, type(exc).__name__)
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise _transport_error(exc, request) from exc

            if response.status_code < 400:
                if self.on_response is not None:
                    self.on_response(response)
                return response
            if key is not None and _in_progress(response):
                # The first attempt is still running on the server: wait for it, for
                # as long as a single request may take, without spending a retry.
                if in_progress_until is None:
                    in_progress_until = time.monotonic() + _wait_budget(self.config)
                if time.monotonic() < in_progress_until:
                    delay = _retry_delay(0, response)
                    _log_retry(request, delay, attempt, "idempotency_in_progress")
                    time.sleep(delay)
                    continue
            if attempt < self.config.max_retries and _should_retry_status(
                _retry_policy(self.config, method, key, response), response.status_code
            ):
                delay = _retry_delay(attempt, response)
                _log_retry(request, delay, attempt, f"HTTP {response.status_code}")
                time.sleep(delay)
                attempt += 1
                continue
            raise make_status_error(response)

    def with_options(
        self,
        timeout: TimeoutTypes,
        max_retries: Optional[int],
        extra_headers: Optional[Mapping[str, str]],
    ) -> SyncHTTP:
        """A view of this transport with per-request overrides (same connection pool)."""
        if timeout is None and max_retries is None and not extra_headers:
            return self
        config = copy.copy(self.config)
        config.headers = {**config.headers, **(extra_headers or {})}
        if timeout is not None:
            config.timeout = timeout
        if max_retries is not None:
            config.max_retries = max_retries
        view = copy.copy(self)
        view.config = config
        view._owns_client = False
        return view

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AsyncHTTP:
    def __init__(self, config: _Config, http_client: Optional[httpx.AsyncClient]) -> None:
        self.config = config
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(timeout=config.timeout or DEFAULT_TIMEOUT)
        self.on_response: Optional[Callable[[httpx.Response], None]] = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Any = None,
        stream: bool = False,
        idempotent: bool = False,
    ) -> httpx.Response:
        headers, key = _idempotency(self.config, method, idempotent)
        attempt = 0
        in_progress_until: Optional[float] = None
        while True:
            request = self.client.build_request(
                method,
                self.config.url(path),
                json=json,
                params=params,
                data=data,
                files=files,
                headers=headers,
                timeout=self.config.request_timeout,
            )
            try:
                started = time.monotonic()
                response = await self.client.send(request, stream=stream)
                _log_response(request, response, started, attempt)
                _retry_policy(self.config, method, key, response)  # learn whether keys are honoured
                if response.status_code >= 400 and stream:
                    try:
                        await response.aread()
                    finally:
                        await response.aclose()
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries and _should_retry_error(
                    _retry_policy(self.config, method, key, None), exc
                ):
                    delay = _retry_delay(attempt, None)
                    _log_retry(request, delay, attempt, type(exc).__name__)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise _transport_error(exc, request) from exc

            if response.status_code < 400:
                if self.on_response is not None:
                    self.on_response(response)
                return response
            if key is not None and _in_progress(response):
                # The first attempt is still running on the server: wait for it, for
                # as long as a single request may take, without spending a retry.
                if in_progress_until is None:
                    in_progress_until = time.monotonic() + _wait_budget(self.config)
                if time.monotonic() < in_progress_until:
                    delay = _retry_delay(0, response)
                    _log_retry(request, delay, attempt, "idempotency_in_progress")
                    await asyncio.sleep(delay)
                    continue
            if attempt < self.config.max_retries and _should_retry_status(
                _retry_policy(self.config, method, key, response), response.status_code
            ):
                delay = _retry_delay(attempt, response)
                _log_retry(request, delay, attempt, f"HTTP {response.status_code}")
                await asyncio.sleep(delay)
                attempt += 1
                continue
            raise make_status_error(response)

    def with_options(
        self,
        timeout: TimeoutTypes,
        max_retries: Optional[int],
        extra_headers: Optional[Mapping[str, str]],
    ) -> AsyncHTTP:
        """A view of this transport with per-request overrides (same connection pool)."""
        if timeout is None and max_retries is None and not extra_headers:
            return self
        config = copy.copy(self.config)
        config.headers = {**config.headers, **(extra_headers or {})}
        if timeout is not None:
            config.timeout = timeout
        if max_retries is not None:
            config.max_retries = max_retries
        view = copy.copy(self)
        view.config = config
        view._owns_client = False
        return view

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
