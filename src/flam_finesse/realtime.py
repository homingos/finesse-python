"""Realtime speech over one WebSocket.

One session is one socket. Each turn of speech runs in a *context*, and a socket can run several
contexts at once (up to 4). Audio arrives as raw PCM, ``s16le`` at 24 kHz unless you ask for
something else::

    async with client.realtime.connect() as rt:
        async for event in rt.say(voice_id=vid, text="Hello there."):
            if isinstance(event, AudioEvent):
                play(event.pcm)

From a browser-facing backend, mint a short-lived token per session instead of exposing the
key (``client.tokens.create``); a client holding only tokens connects with
``client.realtime.connect(get_access_token=...)``, which fetches a fresh one for every
(re)connect.

Stream text as an LLM produces it, and cancel on barge-in::

    ctx = rt.context(voice_id=vid)
    async for token in llm_tokens():
        await ctx.send(token)
    await ctx.finish()
    async for event in ctx:
        ...
    await ctx.cancel()  # stop speaking now; later audio for it is dropped
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import uuid
from types import TracebackType
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Type, Union

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from ._base import API_PREFIX, _Config
from ._errors import APIConnectionError, APITimeoutError, FinesseError, make_status_error

__all__ = [
    "AsyncRealtime",
    "RealtimeSession",
    "RealtimeContext",
    "RealtimeError",
    "RealtimeEvent",
    "TaskStarted",
    "AudioEvent",
    "SentenceDone",
    "TaskDone",
    "TaskCancelled",
]

SUBPROTOCOL = "finesse.v1"
"""The protocol the service selects. A short-lived token travels as a second subprotocol."""

TokenProvider = Callable[[], Union[str, Awaitable[str]]]
"""Returns a fresh access token (``fin_tok_...``); may be async."""

DEFAULT_OPEN_TIMEOUT = 60.0
"""Seconds to open a session. The service accepts the socket, then connects to the engine,
which takes longer when the engine is cold."""
_CLOSE_TIMEOUT = 5.0
_MAX_FRAME = 16 * 1024 * 1024


class RealtimeError(FinesseError):
    """A realtime context or session failed.

    Attributes:
        code: Per context: ``voice_not_found``, ``voice_not_allowed`` (the token is limited to
            other voices), ``invalid_request`` (a parameter out of range), a ``task_failed``
            code. Whole session: ``insufficient_credits``, ``upstream_unavailable`` (the
            engine could not be reached), ``upstream_busy`` (it is at capacity),
            ``upstream_error``, ``upstream_closed``, ``connection_lost``, ``token_spent``, ...
        context_id: The context it applies to, or ``None`` for the whole session.
        close_code: The WebSocket close code, when the socket closed.
        retryable: Whether reconnecting and trying again can succeed.
    """

    _RETRYABLE_CODES = frozenset(
        {
            "upstream_closed",
            "upstream_error",
            "upstream_busy",
            "upstream_unavailable",
            "connection_lost",
            "service_busy",
            "stream_billing_error",
        }
    )

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        context_id: Optional[str] = None,
        close_code: Optional[int] = None,
    ) -> None:
        super().__init__(f"{message} ({code})" if message and message != code else code)
        self.code = code
        self.message = message or code
        self.context_id = context_id
        self.close_code = close_code

    @property
    def retryable(self) -> bool:
        return self.code in self._RETRYABLE_CODES or self.close_code in _RETRYABLE_CLOSES


# Going Away, abnormal closure, internal error, service restart, try again later, engine busy.
_RETRYABLE_CLOSES = frozenset({1001, 1006, 1011, 1012, 1013, 4429})


def _close_error(code: Optional[int], reason: str) -> Optional[RealtimeError]:
    """The error a close code means for contexts still running, or ``None`` for a clean close."""
    if code == 1000:
        return None
    if code == 1008:
        return RealtimeError(reason or "policy_violation", reason, close_code=code)
    if code == 1009:
        return RealtimeError("message_too_big", "A message was too large.", close_code=code)
    if code == 1011:  # reason: upstream_error, upstream_closed or stream_billing_error
        return RealtimeError(reason or "upstream_closed", reason, close_code=code)
    if code == 1013 and reason in ("upstream_busy", "upstream_unavailable"):
        return RealtimeError(
            reason,
            "The synthesis service is at capacity."
            if reason == "upstream_busy"
            else "The synthesis service could not be reached.",
            close_code=code,
        )
    # 4401 / 4429 come only from older services; current ones map them to 1011 / 1013.
    if code == 4401:
        return RealtimeError(
            "service_auth_failed",
            "The service could not start the session (a problem on our side).",
            close_code=code,
        )
    if code == 4429:
        return RealtimeError(
            "service_busy",
            "The service is at its connection limit. Try again shortly.",
            close_code=code,
        )
    if code is None or code == 1006:
        return RealtimeError("connection_lost", "The connection dropped.", close_code=1006)
    if code in (1001, 1012, 1013):
        return RealtimeError(
            "connection_lost",
            "The service closed the connection (restart or overload).",
            close_code=code,
        )
    return RealtimeError(reason or "connection_closed", reason, close_code=code)


class RealtimeEvent(BaseModel):
    """Base of every event a context yields. Unknown engine fields are kept as extras."""

    model_config = ConfigDict(extra="allow")

    type: str
    context_id: Optional[str] = None


class TaskStarted(RealtimeEvent):
    """The context opened. ``sample_rate`` and ``audio_format`` describe its audio."""

    sample_rate: Optional[int] = None
    audio_format: Optional[str] = None


class AudioEvent(RealtimeEvent):
    """A chunk of raw PCM for this context."""

    type: str = "audio"
    pcm: bytes
    sample_rate: Optional[int] = None
    audio_format: Optional[str] = None
    sentence_index: Optional[int] = None
    chunk_index: Optional[int] = None
    audio_duration_s: Optional[float] = None

    def __repr__(self) -> str:
        return (
            f"AudioEvent(context_id={self.context_id!r}, bytes={len(self.pcm)}, "
            f"sentence_index={self.sentence_index!r}, chunk_index={self.chunk_index!r})"
        )


class SentenceDone(RealtimeEvent):
    sentence_index: Optional[int] = None
    text: Optional[str] = None
    audio_duration_s: Optional[float] = None


class TaskDone(RealtimeEvent):
    """The context finished. Last event of a context."""

    num_sentences: Optional[int] = None
    total_audio_duration_s: Optional[float] = None
    profile: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None


class TaskCancelled(RealtimeEvent):
    """The context was cancelled. Last event of a cancelled context."""

    sentences_completed: Optional[int] = None
    sentences_dropped: Optional[int] = None
    trace_id: Optional[str] = None


_EVENTS: Dict[str, Type[RealtimeEvent]] = {
    "task_started": TaskStarted,
    "sentence_done": SentenceDone,
    "task_done": TaskDone,
    "task_cancelled": TaskCancelled,
}
_END = object()


class RealtimeContext:
    """One turn of speech in a session. Create it with :meth:`RealtimeSession.context`.

    ``send`` text (the first call opens the context), ``finish`` when the turn's text is complete,
    and iterate the context for its events. Iteration ends after :class:`TaskDone` or
    :class:`TaskCancelled`, and raises :class:`RealtimeError` if the context fails.

    Events are buffered without limit so that one slow context never stalls the others on the
    same socket. Iterate every context you start, or :meth:`cancel` the ones you abandon.
    """

    def __init__(self, session: RealtimeSession, context_id: str, options: Dict[str, Any]) -> None:
        self.id = context_id
        self._session = session
        self._options = options
        self._queue: asyncio.Queue[Union[RealtimeEvent, RealtimeError, object]] = asyncio.Queue()
        self._started = False
        self._finished = False
        self._ended = False
        self.cancelled = False
        self._send_lock = asyncio.Lock()
        self.sample_rate: Optional[int] = None
        self.audio_format: Optional[str] = None

    async def send(self, text: str, *, finalize: bool = False) -> None:
        """Send text. Text is billed per character as it is sent. Pass ``finalize=True`` with
        the turn's last text (or call :meth:`finish`)."""
        async with self._send_lock:  # keeps task_start ahead of task_continue
            if self._finished:
                raise RealtimeError(
                    "context_finished", "This context was already finalized.", context_id=self.id
                )
            if not self._started:
                self._started = True
                try:
                    await self._session._start(self, text, finalize)
                except FinesseError:
                    self._started = False  # nothing was sent; the context can be retried
                    raise
            else:
                await self._session._send(
                    {
                        "type": "task_continue",
                        "context_id": self.id,
                        "text": text,
                        "finalize": finalize,
                    }
                )
            if finalize:
                self._finished = True

    async def finish(self) -> None:
        """No more text for this turn: speak what is buffered, then end the context."""
        async with self._send_lock:
            if not self._started:
                raise RealtimeError(
                    "context_not_started", "Send text before finishing.", context_id=self.id
                )
            if not self._finished:
                await self._session._send(
                    {"type": "task_continue", "context_id": self.id, "finalize": True}
                )
                self._finished = True

    async def cancel(self) -> None:
        """Stop this context now. Audio still in flight for it is dropped; iterating it ends with
        :class:`TaskCancelled`."""
        if not self._started or self._ended or self.cancelled:
            return
        self.cancelled = True
        self._finished = True
        # A send failure means the socket is gone, which ends the context anyway.
        with contextlib.suppress(RealtimeError):
            await self._session._send({"type": "task_cancel", "context_id": self.id})

    async def __aiter__(self) -> AsyncIterator[RealtimeEvent]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            if isinstance(item, RealtimeError):
                raise item
            assert isinstance(item, RealtimeEvent)
            yield item

    async def audio(self) -> AsyncIterator[bytes]:
        """Iterate only the PCM bytes of this context."""
        async for event in self:
            if isinstance(event, AudioEvent):
                yield event.pcm

    # -- called by the session's reader --

    def _put(self, event: RealtimeEvent) -> None:
        if isinstance(event, TaskStarted):
            self.sample_rate = event.sample_rate
            self.audio_format = event.audio_format
        self._queue.put_nowait(event)
        if event.type in ("task_done", "task_cancelled"):
            self._end()

    def _fail(self, error: RealtimeError) -> None:
        if not self._ended:
            self._queue.put_nowait(error)
            self._end()

    def _end(self) -> None:
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(_END)
            self._session._contexts.pop(self.id, None)


class RealtimeSession:
    """A realtime WebSocket session. Use ``async with client.realtime.connect() as rt``.

    The socket opens on ``async with`` (or the first ``say``/``send``). After a retryable drop
    the next context reconnects automatically, unless ``auto_reconnect=False``. Contexts that
    were running when the socket dropped fail with :class:`RealtimeError`.
    """

    def __init__(
        self,
        config: _Config,
        *,
        auto_reconnect: bool,
        open_timeout: float,
        access_token: Optional[str] = None,
        get_access_token: Optional[TokenProvider] = None,
    ) -> None:
        self._config = config
        self._access_token = access_token or config.access_token
        self._get_access_token = get_access_token
        self._token_spent = False
        self._auto_reconnect = auto_reconnect
        self._open_timeout = open_timeout
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional[asyncio.Task[None]] = None
        self._contexts: Dict[str, RealtimeContext] = {}
        self._send_lock = asyncio.Lock()
        self._open_lock = asyncio.Lock()
        self._pending_audio: Optional[Dict[str, Any]] = None
        self._fatal: Optional[RealtimeError] = None
        self._error: Optional[RealtimeError] = None
        self._opened_once = False
        self._closed = False

    @property
    def url(self) -> str:
        return f"{self._config.ws_base_url}{API_PREFIX}/stream"

    @property
    def is_open(self) -> bool:
        return self._ws is not None and self._reader is not None and not self._reader.done()

    async def open(self) -> None:
        """Open the socket (no-op when already open).

        Raises:
            AuthenticationError, PaymentRequiredError, PermissionDeniedError: the handshake
                was refused (bad key, no credits, plan lacks streaming).
            RateLimitError: ``code == "too_many_sessions"``: the workspace already has its
                maximum of concurrent sessions open.
            APIConnectionError, APITimeoutError: the service could not be reached.
            RealtimeError: the session closed earlier and cannot reconnect.
        """
        async with self._open_lock:
            if self.is_open:
                return
            if self._closed:
                raise RealtimeError("session_closed", "This session was closed.")
            if self._opened_once and not self._auto_reconnect:
                raise self._error or RealtimeError("connection_closed", "The session is closed.")
            token = await self._next_token()
            # A token replaces the key: it rides in the subprotocol list, the one place a
            # browser can put a credential on a WebSocket handshake.
            dropped = {"Accept", "User-Agent"} | (
                {"X-API-Key", "Authorization"} if token else set()
            )
            headers = {k: v for k, v in self._config.headers.items() if k not in dropped}
            subprotocols = [SUBPROTOCOL, token] if token else [SUBPROTOCOL]
            try:
                ws = await connect(
                    self.url,
                    additional_headers=headers,
                    subprotocols=subprotocols,  # type: ignore[arg-type]
                    user_agent_header=self._config.headers.get("User-Agent"),
                    open_timeout=self._open_timeout,
                    max_size=_MAX_FRAME,
                    compression=None,
                )
            except InvalidStatus as exc:
                r = exc.response
                response = httpx.Response(
                    r.status_code,
                    headers=list(r.headers.raw_items()),
                    content=bytes(r.body or b""),
                    request=httpx.Request("GET", self.url),
                )
                raise make_status_error(response) from exc
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise APITimeoutError() from exc
            except (OSError, WebSocketException) as exc:
                raise APIConnectionError(str(exc) or "Connection error.") from exc
            if self._closed:  # close() ran while we were connecting
                await ws.close()
                raise RealtimeError("session_closed", "This session was closed.")
            if token and token == self._access_token:
                self._token_spent = True  # the handshake consumed the static token
            self._ws = ws
            self._pending_audio = None
            self._fatal = None
            self._error = None
            self._opened_once = True
            self._reader = asyncio.ensure_future(self._read(ws))

    async def _next_token(self) -> Optional[str]:
        """The token for the next handshake, or ``None`` to authenticate with the key.

        A static token (``access_token``, or the client's) is used first; once a handshake has
        spent it, ``get_access_token`` supplies a fresh one for each reconnect.
        """
        if self._access_token and not self._token_spent:
            return self._access_token
        if self._get_access_token is not None:
            token = self._get_access_token()
            if inspect.isawaitable(token):
                token = await token
            if not isinstance(token, str) or not token:
                raise FinesseError("get_access_token must return a non-empty token string.")
            return token
        if self._access_token:
            raise RealtimeError(
                "token_spent",
                "This session's access token was used by an earlier connection. Pass "
                "get_access_token=... so a reconnect can fetch a fresh one.",
            )
        return None

    def context(
        self,
        *,
        voice_id: str,
        context_id: Optional[str] = None,
        language: Optional[str] = None,
        output_format: str = "s16le",
        sample_rate: Optional[int] = None,
        num_step: Optional[int] = None,
        guidance_scale: Optional[float] = None,
        length_scale: Optional[float] = None,
        idle_flush_ms: Optional[int] = None,
    ) -> RealtimeContext:
        """Create a context. Nothing is sent until its first :meth:`RealtimeContext.send`.

        Args:
            voice_id: One of your voices or a public platform voice.
            output_format: ``s16le`` (default), ``s24le``, ``s32le``, ``f32le``, ``f64le`` or
                ``opus``.
            sample_rate: Output rate in Hz; the native rate is 24000 and lower values still
                produce 24000 (see ``TaskStarted.sample_rate``).
            length_scale: Speaking rate.
            idle_flush_ms: Speak a pending sentence after this many ms without new text.
        """
        options: Dict[str, Any] = {"voice_id": voice_id, "output_format": output_format}
        for key, value in (
            ("language", language),
            ("sample_rate", sample_rate),
            ("num_step", num_step),
            ("guidance_scale", guidance_scale),
            ("length_scale", length_scale),
            ("idle_flush_ms", idle_flush_ms),
        ):
            if value is not None:
                options[key] = value
        return RealtimeContext(self, context_id or uuid.uuid4().hex, options)

    async def say(
        self, *, voice_id: str, text: str, **options: Any
    ) -> AsyncIterator[RealtimeEvent]:
        """Speak ``text`` as one complete turn and yield its events. Takes the same options as
        :meth:`context`.

        If you stop iterating early, the turn is cancelled when the generator is closed. Close
        it promptly with ``contextlib.aclosing(rt.say(...))``.
        """
        ctx = self.context(voice_id=voice_id, **options)
        await ctx.send(text, finalize=True)
        try:
            async for event in ctx:
                yield event
        finally:
            if not ctx._ended:
                await ctx.cancel()

    async def close(self) -> None:
        """End the session: tell the service, then close the socket."""
        self._closed = True
        ws = self._ws
        if ws is not None and self.is_open:
            with contextlib.suppress(ConnectionClosed):
                async with self._send_lock:
                    await ws.send(json.dumps({"type": "session_end"}))
            if self._reader is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._reader), _CLOSE_TIMEOUT)
        if ws is not None:
            await ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)

    async def __aenter__(self) -> RealtimeSession:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    # -- internals --

    async def _start(self, ctx: RealtimeContext, text: str, finalize: bool) -> None:
        await self.open()
        live = self._contexts.get(ctx.id)
        if live is not None and live is not ctx:
            raise RealtimeError(
                "context_already_exists",
                "A running context already uses this id.",
                context_id=ctx.id,
            )
        msg: Dict[str, Any] = {"type": "task_start", "context_id": ctx.id, **ctx._options}
        msg["text"] = text
        msg["finalize"] = finalize
        # Register before sending so no reply can arrive for an unknown context. If the send
        # fails the frame never left, so forget the context. A cancellation mid-send keeps it,
        # because the server may already be working on it.
        self._contexts[ctx.id] = ctx
        try:
            await self._send(msg)
        except FinesseError:
            self._contexts.pop(ctx.id, None)
            raise

    async def _send(self, msg: Dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or not self.is_open:
            raise self._error or RealtimeError("connection_closed", "The session is not open.")
        try:
            async with self._send_lock:
                await ws.send(json.dumps(msg))
        except ConnectionClosed as exc:
            raise (
                self._error
                or _close_error(
                    exc.rcvd.code if exc.rcvd else None, exc.rcvd.reason if exc.rcvd else ""
                )
                or RealtimeError("connection_closed", "The session is closed.")
            ) from exc

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for frame in ws:
                try:
                    if isinstance(frame, bytes):
                        self._on_audio(frame)
                    else:
                        self._on_message(frame)
                except Exception:
                    continue  # a frame we cannot parse must not end the session
        except ConnectionClosed:
            pass
        finally:
            # Always release the socket, even if the reader stopped for an unexpected reason.
            with contextlib.suppress(Exception):
                await asyncio.shield(ws.close())
            error = self._fatal
            if error is not None:
                if error.close_code is None:
                    error.close_code = ws.close_code
            else:
                error = _close_error(ws.close_code, ws.close_reason or "")
            if error is None and self._contexts:
                error = RealtimeError(
                    "connection_closed", "The session ended.", close_code=ws.close_code
                )
            self._error = error
            for ctx in list(self._contexts.values()):
                ctx._fail(error or RealtimeError("connection_closed"))

    def _on_audio(self, frame: bytes) -> None:
        header, self._pending_audio = self._pending_audio, None
        if header is None:
            return
        ctx = self._contexts.get(str(header.get("context_id")))
        if ctx is None or ctx.cancelled:
            return
        try:
            event = AudioEvent(
                context_id=ctx.id,
                pcm=frame,
                sample_rate=ctx.sample_rate,
                audio_format=ctx.audio_format,
                sentence_index=header.get("sentence_index"),
                chunk_index=header.get("chunk_index"),
                audio_duration_s=header.get("audio_duration_s"),
            )
        except ValidationError:  # odd header fields: keep the audio, drop the metadata
            event = AudioEvent(
                context_id=ctx.id,
                pcm=frame,
                sample_rate=ctx.sample_rate,
                audio_format=ctx.audio_format,
            )
        ctx._put(event)

    def _on_message(self, frame: str) -> None:
        try:
            data = json.loads(frame)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        if kind == "audio_chunk":
            self._pending_audio = data
            return
        cid = data.get("context_id")
        ctx = self._contexts.get(cid) if isinstance(cid, str) else None
        if kind == "error":
            err = RealtimeError(
                str(data.get("code") or "error"), str(data.get("message") or ""), context_id=cid
            )
            if data.get("fatal"):
                self._fatal = self._fatal or err
            elif ctx is not None:
                ctx._fail(err)
            # A non-fatal error for a context that already ended is stale: drop it.
            return
        if kind == "session_ended":
            reason = str(data.get("reason") or "")
            self._fatal = self._fatal or RealtimeError(
                "session_ended", reason or "The session ended."
            )
            return
        if ctx is None or not isinstance(kind, str):
            return
        if kind == "task_failed":
            ctx._fail(
                RealtimeError(
                    str(data.get("code") or "task_failed"),
                    str(data.get("message") or ""),
                    context_id=ctx.id,
                )
            )
            return
        model = _EVENTS.get(str(kind), RealtimeEvent)
        try:
            event = model.model_validate(data)
        except ValidationError:
            # Odd field types: keep the event (and so the end of the turn) without them.
            event = RealtimeEvent(type=str(kind), context_id=ctx.id)
        ctx._put(event)


class AsyncRealtime:
    """``client.realtime``: open realtime sessions."""

    def __init__(self, config: _Config) -> None:
        self._config = config

    def connect(
        self,
        *,
        auto_reconnect: bool = True,
        open_timeout: float = DEFAULT_OPEN_TIMEOUT,
        access_token: Optional[str] = None,
        get_access_token: Optional[TokenProvider] = None,
    ) -> RealtimeSession:
        """Create a session; it opens on ``async with`` or first use.

        Args:
            access_token: A short-lived token (``fin_tok_...``) to authenticate with instead
                of the API key. Single use: it is spent by the handshake, so a reconnect needs
                ``get_access_token``.
            get_access_token: Called for a fresh token before every (re)connect, e.g. a call
                to your own backend that runs ``tokens.create``. May be async.
        """
        return RealtimeSession(
            self._config,
            auto_reconnect=auto_reconnect,
            open_timeout=open_timeout,
            access_token=access_token,
            get_access_token=get_access_token,
        )
