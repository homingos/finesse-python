from __future__ import annotations

from types import TracebackType
from typing import Any, Dict, Mapping, Optional, Type

import httpx

from ._base import DEFAULT_MAX_RETRIES, AsyncHTTP, SyncHTTP, TimeoutTypes, _Config
from ._raw import RawClient
from .realtime import AsyncRealtime
from .resources import AsyncJobs, AsyncSpeech, AsyncVoices, Jobs, Speech, Voices


class Finesse:
    """Client for the Finesse API.

    Args:
        api_key: Workspace API key (``flv_...``). Defaults to ``FINESSE_API_KEY``.
        base_url: API host. Defaults to ``FINESSE_BASE_URL``, then production.
        timeout: Seconds, or an ``httpx.Timeout``. Default 120 s (10 s to connect), or the
            timeout of your ``http_client``.
        max_retries: Retries for transient failures. Default 2. Reads, updates and deletes
            retry on connection errors, timeouts, 408, 429 and 5xx. Creates, uploads and
            synthesis retry only when the server did no work: the connection never opened,
            429, 502 or 503.
        default_headers: Extra headers sent on every request.
        http_client: Your own ``httpx.Client`` (proxies, custom transports). You close it.

    Example::

        from flam_finesse import Finesse

        client = Finesse()
        audio = client.speech.create(voice_id="665f1a2b3c4d5e6f7a8b9c0d", text="Hello!")
        audio.save("hello.wav")
    """

    voices: Voices
    jobs: Jobs
    speech: Speech

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._config = _Config(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._http = SyncHTTP(self._config, http_client)
        self._attach()

    def _attach(self) -> None:
        resources = _sync_resources(self._http)
        self.jobs, self.voices, self.speech = (
            resources["jobs"],
            resources["voices"],
            resources["speech"],
        )

    def with_options(
        self,
        *,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> Finesse:
        """A copy of this client with different defaults. It shares the connection pool, so
        it is cheap to make; close the original, not the copy."""
        clone = object.__new__(Finesse)
        clone._http = self._http.with_options(timeout, max_retries, default_headers)
        clone._config = clone._http.config
        clone._attach()
        return clone

    @property
    def with_raw_response(self) -> RawClient:
        """The same calls, each returning a :class:`~flam_finesse.RawResponse` with the HTTP
        status and headers::

            raw = client.with_raw_response.voices.get(voice_id)
            print(raw.status_code, raw.request_id)
            voice = raw.parse()
        """
        return RawClient(self._http, _sync_resources, is_async=False)

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def close(self) -> None:
        """Close the underlying HTTP connections (not a client you passed in)."""
        self._http.close()

    def __enter__(self) -> Finesse:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


class AsyncFinesse:
    """Async client for the Finesse API. Takes the same arguments as :class:`Finesse`
    (``http_client`` is an ``httpx.AsyncClient``)."""

    voices: AsyncVoices
    jobs: AsyncJobs
    speech: AsyncSpeech
    realtime: AsyncRealtime

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: TimeoutTypes = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        default_headers: Optional[Mapping[str, str]] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._config = _Config(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            default_headers=default_headers,
        )
        self._http = AsyncHTTP(self._config, http_client)
        self._attach()

    def _attach(self) -> None:
        resources = _async_resources(self._http)
        self.jobs, self.voices, self.speech = (
            resources["jobs"],
            resources["voices"],
            resources["speech"],
        )
        self.realtime = AsyncRealtime(self._http.config)

    def with_options(
        self,
        *,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> AsyncFinesse:
        """A copy of this client with different defaults, sharing the connection pool."""
        clone = object.__new__(AsyncFinesse)
        clone._http = self._http.with_options(timeout, max_retries, default_headers)
        clone._config = clone._http.config
        clone._attach()
        return clone

    @property
    def with_raw_response(self) -> RawClient:
        """The same calls, each returning a :class:`~flam_finesse.RawResponse` (await them)."""
        return RawClient(self._http, _async_resources, is_async=True)

    @property
    def base_url(self) -> str:
        return self._config.base_url

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> AsyncFinesse:
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.close()


def _sync_resources(http: Any) -> Dict[str, Any]:
    jobs = Jobs(http)
    return {"jobs": jobs, "voices": Voices(http, jobs), "speech": Speech(http)}


def _async_resources(http: Any) -> Dict[str, Any]:
    jobs = AsyncJobs(http)
    return {"jobs": jobs, "voices": AsyncVoices(http, jobs), "speech": AsyncSpeech(http)}
