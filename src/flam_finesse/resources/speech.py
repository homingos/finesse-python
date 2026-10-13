from __future__ import annotations

import base64
import binascii
import json
from types import TracebackType
from typing import Any, AsyncIterator, Dict, Iterator, Mapping, Optional, Type

import httpx

from .._base import AsyncHTTP, SyncHTTP, TimeoutTypes, _transport_error
from .._errors import FinesseError, StreamError
from .._sse import SSEEvent, aiter_sse, iter_sse
from .._util import path_id
from ..types import Audio, AudioChunk, StreamDone


def _speech_body(
    text: str,
    language: Optional[str],
    output_format: Optional[str],
    sample_rate: Optional[int],
    num_step: Optional[int],
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"text": text}
    if language is not None:
        body["language"] = language
    if output_format is not None:
        body["output_format"] = output_format
    if sample_rate is not None:
        body["sample_rate"] = sample_rate
    if num_step is not None:
        body["num_step"] = num_step
    return body


def _int_header(response: httpx.Response, name: str) -> Optional[int]:
    value = response.headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _audio(response: httpx.Response) -> Audio:
    return Audio(
        data=response.content,
        content_type=response.headers.get("Content-Type", "audio/wav"),
        sample_rate=_int_header(response, "X-Sample-Rate"),
        generation_id=response.headers.get("X-Generation-Id") or None,
        audio_url=response.headers.get("X-Audio-URL") or None,
        billing_remaining=_int_header(response, "X-Billing-Remaining"),
    )


class _StreamState:
    """Turns SSE events into chunks; shared by the sync and async streams."""

    def __init__(self) -> None:
        self.sample_rate: Optional[int] = None
        self.audio_format: Optional[str] = None
        self.done: Optional[StreamDone] = None
        self.billing_remaining: Optional[int] = None

    def handle(self, ev: SSEEvent) -> Optional[AudioChunk]:
        """Return a chunk for ``chunk`` events, ``None`` otherwise. Raises on ``error``."""
        try:
            payload = json.loads(ev.data) if ev.data else {}
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        kind = payload.get("type") or ev.event
        if kind == "start":
            self.sample_rate = payload.get("sample_rate", self.sample_rate)
            self.audio_format = payload.get("audio_format", self.audio_format)
        elif kind == "chunk":
            try:
                pcm = base64.b64decode(payload.get("audio_b64") or "", validate=True)
            except (binascii.Error, ValueError) as exc:
                raise StreamError(
                    "bad_chunk", "The stream sent an undecodable audio chunk."
                ) from exc
            return AudioChunk(
                pcm=pcm,
                sample_rate=payload.get("sample_rate", self.sample_rate),
                audio_format=payload.get("audio_format", self.audio_format),
                index=payload.get("index"),
            )
        elif kind == "done":
            self.done = StreamDone.model_validate(payload)
        elif kind == "error":
            raise StreamError(payload.get("code") or "stream_error", payload.get("message") or "")
        return None

    def finish(self) -> None:
        if self.done is None:
            raise StreamError("stream_incomplete", "The stream ended before it finished.")


class SpeechStream:
    """Speech streamed as raw PCM chunks. Iterate it; ``done`` is set once it finishes.

    The request is sent when iteration starts (or on ``with``). Errors before the stream starts
    raise the usual :class:`~flam_finesse.APIStatusError`; failures after it raise
    :class:`~flam_finesse.StreamError`.
    """

    def __init__(self, http: SyncHTTP, path: str, body: Dict[str, Any]) -> None:
        self._http = http
        self._path = path
        self._body = body
        self._response: Optional[httpx.Response] = None
        self._state = _StreamState()
        self._consumed = False

    @property
    def sample_rate(self) -> Optional[int]:
        return self._state.sample_rate

    @property
    def audio_format(self) -> Optional[str]:
        return self._state.audio_format

    @property
    def done(self) -> Optional[StreamDone]:
        """Final event (duration, generation id, stored clip URL), once the stream finished."""
        return self._state.done

    @property
    def billing_remaining(self) -> Optional[int]:
        return self._state.billing_remaining

    def _open(self) -> httpx.Response:
        if self._response is None:
            self._response = self._http.request("POST", self._path, json=self._body, stream=True)
            self._state.billing_remaining = _int_header(self._response, "X-Billing-Remaining")
        return self._response

    def __iter__(self) -> Iterator[AudioChunk]:
        if self._consumed:
            raise FinesseError("This stream was already consumed; start a new one.")
        self._consumed = True
        response = self._open()
        try:
            for ev in iter_sse(response.iter_lines()):
                chunk = self._state.handle(ev)
                if chunk is not None:
                    yield chunk
            self._state.finish()
        except httpx.TransportError as exc:
            raise _transport_error(exc, response.request) from exc
        finally:
            self.close()

    def read_all(self) -> bytes:
        """Consume the stream and return all PCM joined together."""
        return b"".join(chunk.pcm for chunk in self)

    def close(self) -> None:
        if self._response is not None:
            self._response.close()

    def __enter__(self) -> SpeechStream:
        self._open()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()


class AsyncSpeechStream:
    """Async counterpart of :class:`SpeechStream`: ``async for chunk in stream``."""

    def __init__(self, http: AsyncHTTP, path: str, body: Dict[str, Any]) -> None:
        self._http = http
        self._path = path
        self._body = body
        self._response: Optional[httpx.Response] = None
        self._state = _StreamState()
        self._consumed = False

    @property
    def sample_rate(self) -> Optional[int]:
        return self._state.sample_rate

    @property
    def audio_format(self) -> Optional[str]:
        return self._state.audio_format

    @property
    def done(self) -> Optional[StreamDone]:
        return self._state.done

    @property
    def billing_remaining(self) -> Optional[int]:
        return self._state.billing_remaining

    async def _open(self) -> httpx.Response:
        if self._response is None:
            self._response = await self._http.request(
                "POST", self._path, json=self._body, stream=True
            )
            self._state.billing_remaining = _int_header(self._response, "X-Billing-Remaining")
        return self._response

    async def __aiter__(self) -> AsyncIterator[AudioChunk]:
        if self._consumed:
            raise FinesseError("This stream was already consumed; start a new one.")
        self._consumed = True
        response = await self._open()
        try:
            async for ev in aiter_sse(response.aiter_lines()):
                chunk = self._state.handle(ev)
                if chunk is not None:
                    yield chunk
            self._state.finish()
        except httpx.TransportError as exc:
            raise _transport_error(exc, response.request) from exc
        finally:
            await self.close()

    async def read_all(self) -> bytes:
        return b"".join([chunk.pcm async for chunk in self])

    async def close(self) -> None:
        if self._response is not None:
            await self._response.aclose()

    async def __aenter__(self) -> AsyncSpeechStream:
        await self._open()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.close()


class Speech:
    """Text-to-speech."""

    def __init__(self, http: SyncHTTP) -> None:
        self._http = http

    def create(
        self,
        *,
        voice_id: str,
        text: str,
        language: Optional[str] = None,
        output_format: Optional[str] = None,
        sample_rate: Optional[int] = None,
        num_step: Optional[int] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Audio:
        """Synthesize ``text`` (at most 10,000 characters) and return the whole clip.

        Args:
            voice_id: One of your voices or a public platform voice; it must be ready.
            output_format: ``wav`` (default) or raw PCM: ``s16le``, ``s24le``, ``s32le``,
                ``f32le``, ``f64le``.
            sample_rate: Output rate in Hz. The native rate is 24000; lower values still
                return 24000 (check ``Audio.sample_rate``).
            num_step: Generation steps (default 32); fewer is faster, more is higher quality.
        """
        body = _speech_body(text, language, output_format, sample_rate, num_step)
        response = self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", f"/voices/{path_id(voice_id)}/speak", json=body
        )
        return _audio(response)

    def stream(
        self,
        *,
        voice_id: str,
        text: str,
        language: Optional[str] = None,
        output_format: Optional[str] = None,
        sample_rate: Optional[int] = None,
        num_step: Optional[int] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> SpeechStream:
        """Stream speech as raw PCM chunks (``s16le`` unless ``output_format`` says otherwise)::

        with client.speech.stream(voice_id=vid, text="Hello") as stream:
            for chunk in stream:
                play(chunk.pcm)
        """
        body = _speech_body(text, language, output_format, sample_rate, num_step)
        return SpeechStream(
            self._http.with_options(timeout, max_retries, extra_headers),
            f"/voices/{path_id(voice_id)}/stream",
            body,
        )


class AsyncSpeech:
    """Async counterpart of :class:`Speech`."""

    def __init__(self, http: AsyncHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        voice_id: str,
        text: str,
        language: Optional[str] = None,
        output_format: Optional[str] = None,
        sample_rate: Optional[int] = None,
        num_step: Optional[int] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Audio:
        body = _speech_body(text, language, output_format, sample_rate, num_step)
        response = await self._http.with_options(timeout, max_retries, extra_headers).request(
            "POST", f"/voices/{path_id(voice_id)}/speak", json=body
        )
        return _audio(response)

    def stream(
        self,
        *,
        voice_id: str,
        text: str,
        language: Optional[str] = None,
        output_format: Optional[str] = None,
        sample_rate: Optional[int] = None,
        num_step: Optional[int] = None,
        timeout: TimeoutTypes = None,
        max_retries: Optional[int] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> AsyncSpeechStream:
        """``async with client.speech.stream(...) as stream: async for chunk in stream: ...``"""
        body = _speech_body(text, language, output_format, sample_rate, num_step)
        return AsyncSpeechStream(
            self._http.with_options(timeout, max_retries, extra_headers),
            f"/voices/{path_id(voice_id)}/stream",
            body,
        )
