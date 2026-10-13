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

# 408/409 are transient; 429 is the engine's rate limit; 5xx are server or engine hiccups.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
_MAX_BACKOFF = 8.0

TimeoutTypes = Union[float, httpx.Timeout, None]


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
        self.client = http_client or httpx.Client(timeout=config.timeout)

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
                timeout=self.config.timeout,
            )
            try:
                response = self.client.send(request, stream=stream)
            except httpx.TimeoutException as exc:
                if attempt < self.config.max_retries:
                    time.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise APITimeoutError(request=request) from exc
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries:
                    time.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise APIConnectionError(str(exc) or "Connection error.", request=request) from exc

            if response.status_code < 400:
                return response
            if stream:
                response.read()
                response.close()
            if response.status_code in _RETRY_STATUSES and attempt < self.config.max_retries:
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
        self.client = http_client or httpx.AsyncClient(timeout=config.timeout)

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
                timeout=self.config.timeout,
            )
            try:
                response = await self.client.send(request, stream=stream)
            except httpx.TimeoutException as exc:
                if attempt < self.config.max_retries:
                    await asyncio.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise APITimeoutError(request=request) from exc
            except httpx.TransportError as exc:
                if attempt < self.config.max_retries:
                    await asyncio.sleep(_retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise APIConnectionError(str(exc) or "Connection error.", request=request) from exc

            if response.status_code < 400:
                return response
            if stream:
                await response.aread()
                await response.aclose()
            if response.status_code in _RETRY_STATUSES and attempt < self.config.max_retries:
                await asyncio.sleep(_retry_delay(attempt, response))
                attempt += 1
                continue
            raise make_status_error(response)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
