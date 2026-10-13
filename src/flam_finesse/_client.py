from __future__ import annotations

from types import TracebackType
from typing import Mapping, Optional, Type

import httpx

from ._base import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT, AsyncHTTP, SyncHTTP, TimeoutTypes, _Config
from .resources import AsyncJobs, AsyncSpeech, AsyncVoices, Jobs, Speech, Voices


class Finesse:
    """Client for the Finesse API.

    Args:
        api_key: Workspace API key (``flv_...``). Defaults to ``FINESSE_API_KEY``.
        base_url: API host. Defaults to ``FINESSE_BASE_URL``, then production.
        timeout: Seconds, or an ``httpx.Timeout``. Default 120 s (10 s to connect).
        max_retries: Retries for connection errors, timeouts, 408/409/429 and 5xx. Default 2.
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
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
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
        self.jobs = Jobs(self._http)
        self.voices = Voices(self._http, self.jobs)
        self.speech = Speech(self._http)

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

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: TimeoutTypes = DEFAULT_TIMEOUT,
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
        self.jobs = AsyncJobs(self._http)
        self.voices = AsyncVoices(self._http, self.jobs)
        self.speech = AsyncSpeech(self._http)

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
