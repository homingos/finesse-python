"""HTTP plumbing shared by the sync and async clients: auth, retries, error mapping."""

from __future__ import annotations

import asyncio
import email.utils
import os
import platform
import random
import time
from typing import Any, Dict, Mapping, Optional, Union

import httpx

from ._errors import APIConnectionError, APITimeoutError, FinesseError, make_status_error
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
    ) -> None:
        api_key = api_key if api_key is not None else os.environ.get("FINESSE_API_KEY")
        if not api_key:
            raise FinesseError(
                "No API key. Pass api_key=... or set the FINESSE_API_KEY environment variable."
            )
        self.api_key = api_key
        base = base_url or os.environ.get("FINESSE_BASE_URL") or DEFAULT_BASE_URL
        self.base_url = base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers: Dict[str, str] = {
            "X-API-Key": api_key,
            "User-Agent": f"{CLIENT_NAME} python/{platform.python_version()}",
            "X-Finesse-Client": CLIENT_NAME,
            "Accept": "application/json",
        }
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


def _should_retry_error(method: str, exc: Exception) -> bool:
    return method != "POST" or isinstance(exc, _UNSENT_ERRORS)


def _should_retry_status(method: str, status: int) -> bool:
    return status in (_POST_RETRY_STATUSES if method == "POST" else _RETRY_STATUSES)


def _transport_error(exc: httpx.TransportError, request: httpx.Request) -> APIConnectionError:
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError(request=request)
    return APIConnectionError(str(exc) or "Connection error.", request=request)


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
    ) -> httpx.Response:
        """Send a request, retrying transient failures, and raise on an error status.

        With ``stream=True`` the caller must close the returned response.
        """
        attempt = 0
        while True:
            request = self.client.build_request(
                method,
                self.config.url(path),
                json=json,
                params=params,
                data=data,
                files=files,
                headers=self.config.headers,
                timeout=self.config.request_timeout,
            )
            try:
                response = self.client.send(request, stream=stream)
                if response.status_code >= 400 and stream:
                    try:
                        response.read()
                    finally:
                        response.close()
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries and _should_retry_error(method, exc):
                    time.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise _transport_error(exc, request) from exc

            if response.status_code < 400:
                return response
            if attempt < self.config.max_retries and _should_retry_status(
                method, response.status_code
            ):
                time.sleep(_retry_delay(attempt, response))
                attempt += 1
                continue
            raise make_status_error(response)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


class AsyncHTTP:
    def __init__(self, config: _Config, http_client: Optional[httpx.AsyncClient]) -> None:
        self.config = config
        self._owns_client = http_client is None
        self.client = http_client or httpx.AsyncClient(timeout=config.timeout or DEFAULT_TIMEOUT)

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
    ) -> httpx.Response:
        attempt = 0
        while True:
            request = self.client.build_request(
                method,
                self.config.url(path),
                json=json,
                params=params,
                data=data,
                files=files,
                headers=self.config.headers,
                timeout=self.config.request_timeout,
            )
            try:
                response = await self.client.send(request, stream=stream)
                if response.status_code >= 400 and stream:
                    try:
                        await response.aread()
                    finally:
                        await response.aclose()
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries and _should_retry_error(method, exc):
                    await asyncio.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise _transport_error(exc, request) from exc

            if response.status_code < 400:
                return response
            if attempt < self.config.max_retries and _should_retry_status(
                method, response.status_code
            ):
                await asyncio.sleep(_retry_delay(attempt, response))
                attempt += 1
                continue
            raise make_status_error(response)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
