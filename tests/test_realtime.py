from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

import flam_finesse as ff
from flam_finesse import AsyncFinesse

VID = "665f1a2b3c4d5e6f7a8b9c0d"
PATH = "/flam-voice-svc/api/v1/public/stream"


class Engine:
    """A fake service: answers each task with audio, and records what it received."""

    def __init__(self) -> None:
        self.received: List[Dict[str, Any]] = []
        self.headers: List[Headers] = []
        self.paths: List[str] = []
        self.connections = 0
        self.cancelled: set = set()

    async def audio(self, ws: ServerConnection, cid: str, pcm: bytes, i: int = 0) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "audio_chunk",
                    "context_id": cid,
                    "sentence_index": 0,
                    "chunk_index": i,
                    "byte_length": len(pcm),
                }
            )
        )
        await ws.send(pcm)

    async def on_message(self, ws: ServerConnection, msg: Dict[str, Any]) -> None:
        cid = msg.get("context_id", "")
        kind = msg["type"]
        if kind == "task_start":
            await ws.send(
                json.dumps(
                    {
                        "type": "task_started",
                        "context_id": cid,
                        "sample_rate": 24000,
                        "audio_format": msg.get("output_format"),
                    }
                )
            )
        if kind in ("task_start", "task_continue"):
            if msg.get("text"):
                await self.audio(ws, cid, msg["text"].encode())
            if msg.get("finalize"):
                await ws.send(
                    json.dumps(
                        {
                            "type": "sentence_done",
                            "context_id": cid,
                            "sentence_index": 0,
                            "text": "x",
                            "audio_duration_s": 0.5,
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "task_done",
                            "context_id": cid,
                            "num_sentences": 1,
                            "total_audio_duration_s": 0.5,
                        }
                    )
                )
        elif kind == "task_cancel":
            self.cancelled.add(cid)
            await self.audio(ws, cid, b"late")  # in flight when the cancel lands: must be dropped
            await ws.send(
                json.dumps(
                    {
                        "type": "task_cancelled",
                        "context_id": cid,
                        "sentences_completed": 0,
                        "sentences_dropped": 1,
                    }
                )
            )
        elif kind == "session_end":
            await ws.send(json.dumps({"type": "session_ended", "reason": "client_request"}))
            await ws.close(1000)

    async def handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        self.headers.append(ws.request.headers)
        self.paths.append(ws.request.path)
        with contextlib.suppress(ConnectionClosed):
            async for raw in ws:
                msg = json.loads(raw)
                self.received.append(msg)
                await self.on_message(ws, msg)


@contextlib.asynccontextmanager
async def running(
    engine: Engine, process_request: Optional[Callable[[ServerConnection, Request], Any]] = None
) -> AsyncIterator[AsyncFinesse]:
    async with serve(engine.handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        client = AsyncFinesse(api_key="flv_test", base_url=f"http://127.0.0.1:{port}")
        yield client


async def collect(it: AsyncIterator[ff.RealtimeEvent]) -> List[ff.RealtimeEvent]:
    return [e async for e in it]


async def test_say() -> None:
    engine = Engine()
    async with running(engine) as client, client.realtime.connect() as rt:
        events = await collect(rt.say(voice_id=VID, text="Hello.", length_scale=1.1))
    assert [e.type for e in events] == ["task_started", "audio", "sentence_done", "task_done"]
    audio = events[1]
    assert isinstance(audio, ff.AudioEvent)
    assert (audio.pcm, audio.sample_rate, audio.audio_format) == (b"Hello.", 24000, "s16le")
    assert isinstance(events[-1], ff.TaskDone) and events[-1].num_sentences == 1
    start = engine.received[0]
    assert start["type"] == "task_start" and start["voice_id"] == VID
    assert (start["text"], start["finalize"], start["output_format"], start["length_scale"]) == (
        "Hello.",
        True,
        "s16le",
        1.1,
    )
    assert engine.received[-1] == {"type": "session_end"}
    h = engine.headers[0]
    assert h["X-API-Key"] == "flv_test"
    assert h["X-Finesse-Client"].startswith("flam-finesse-python/")
    assert h["User-Agent"].startswith("flam-finesse-python/")
    assert "api_key" not in engine.paths[0]
    assert engine.paths[0] == PATH


async def test_streamed_text() -> None:
    engine = Engine()
    async with running(engine) as client, client.realtime.connect() as rt:
        ctx = rt.context(voice_id=VID, idle_flush_ms=300)
        await ctx.send("Hello ")
        await ctx.send("world.")
        await ctx.finish()
        pcm = [chunk async for chunk in ctx.audio()]
    assert pcm == [b"Hello ", b"world."]
    kinds = [(m["type"], m.get("text"), m.get("finalize")) for m in engine.received[:3]]
    assert kinds == [
        ("task_start", "Hello ", False),
        ("task_continue", "world.", False),
        ("task_continue", None, True),
    ]
    assert engine.received[0]["idle_flush_ms"] == 300
    assert ctx.sample_rate == 24000


async def test_concurrent_contexts_are_demultiplexed() -> None:
    engine = Engine()
    async with running(engine) as client, client.realtime.connect() as rt:
        a = rt.context(voice_id=VID, context_id="a")
        b = rt.context(voice_id=VID, context_id="b")
        await a.send("A1")
        await b.send("B1")
        await a.send("A2", finalize=True)
        await b.send("B2", finalize=True)
        got_a, got_b = await asyncio.gather(collect(a.audio()), collect(b.audio()))
    assert got_a == [b"A1", b"A2"]
    assert got_b == [b"B1", b"B2"]


async def test_cancel_drops_late_audio() -> None:
    engine = Engine()
    async with running(engine) as client, client.realtime.connect() as rt:
        ctx = rt.context(voice_id=VID)
        await ctx.send("first ")
        events = []
        async for event in ctx:
            events.append(event)
            if event.type == "audio":
                await ctx.cancel()
    assert [e.type for e in events] == ["task_started", "audio", "task_cancelled"]
    assert [e.pcm for e in events if isinstance(e, ff.AudioEvent)] == [b"first "]
    assert isinstance(events[-1], ff.TaskCancelled) and events[-1].sentences_dropped == 1
    assert ctx.id in engine.cancelled
    await ctx.cancel()  # idempotent


async def test_say_cancels_when_abandoned() -> None:
    engine = Engine()

    async def slow(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        if msg["type"] == "task_start":
            await engine.audio(ws, msg["context_id"], b"one")
        else:
            await Engine.on_message(engine, ws, msg)

    engine.on_message = slow  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        gen = rt.say(voice_id=VID, text="long")
        async for _ in gen:
            break
        await gen.aclose()
        await asyncio.sleep(0.05)
    assert [m["type"] for m in engine.received[:2]] == ["task_start", "task_cancel"]


async def test_non_fatal_context_error() -> None:
    engine = Engine()

    async def reject_first(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        if msg["type"] == "task_start" and msg["context_id"] == "bad":
            await ws.send(
                json.dumps(
                    {
                        "type": "error",
                        "context_id": "bad",
                        "code": "voice_not_found",
                        "message": "Voice not found",
                    }
                )
            )
            return
        await Engine.on_message(engine, ws, msg)

    engine.on_message = reject_first  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        bad = rt.context(voice_id="nope", context_id="bad")
        await bad.send("x", finalize=True)
        with pytest.raises(ff.RealtimeError) as info:
            await collect(bad)
        assert info.value.code == "voice_not_found" and info.value.context_id == "bad"
        assert not info.value.retryable
        events = await collect(rt.say(voice_id=VID, text="still works"))
        assert events[-1].type == "task_done"


async def test_task_failed() -> None:
    engine = Engine()

    async def fail(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        if msg["type"] == "task_start":
            await ws.send(
                json.dumps(
                    {
                        "type": "task_failed",
                        "context_id": msg["context_id"],
                        "code": "text_too_long",
                        "message": "too long",
                    }
                )
            )
        else:
            await Engine.on_message(engine, ws, msg)

    engine.on_message = fail  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        with pytest.raises(ff.RealtimeError) as info:
            await collect(rt.say(voice_id=VID, text="x"))
    assert info.value.code == "text_too_long"


def closing_with(
    error: Optional[Dict[str, Any]], code: int, reason: str
) -> Callable[..., Awaitable[None]]:
    async def on_message(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        if msg["type"] == "task_start":
            if error:
                await ws.send(json.dumps({"type": "error", **error}))
            await ws.close(code, reason)

    return on_message


@pytest.mark.parametrize(
    ("error", "close", "reason", "code", "retryable"),
    [
        (
            {"code": "insufficient_credits", "message": "Out of credits", "fatal": True},
            1008,
            "insufficient_credits",
            "insufficient_credits",
            False,
        ),
        (
            {"code": "upstream_closed", "message": "engine dropped", "fatal": True},
            1011,
            "upstream_closed",
            "upstream_closed",
            True,
        ),
        (None, 4429, "too many connections", "service_busy", True),
        (None, 4401, "bad engine key", "service_auth_failed", False),
        (None, 1011, "", "upstream_closed", True),
    ],
)
async def test_fatal_closes(
    error: Any, close: int, reason: str, code: str, retryable: bool
) -> None:
    engine = Engine()
    engine.on_message = closing_with(error, close, reason)  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        with pytest.raises(ff.RealtimeError) as info:
            await collect(rt.say(voice_id=VID, text="x"))
    e = info.value
    assert (e.code, e.close_code, e.retryable) == (code, close, retryable)


async def test_dropped_connection_then_reconnect() -> None:
    engine = Engine()
    first = True

    async def drop_once(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        nonlocal first
        if first and msg["type"] == "task_start":
            first = False
            ws.transport.abort()  # no close frame: the client sees 1006
            return
        await Engine.on_message(engine, ws, msg)

    engine.on_message = drop_once  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        with pytest.raises(ff.RealtimeError) as info:
            await collect(rt.say(voice_id=VID, text="x"))
        assert (info.value.code, info.value.close_code, info.value.retryable) == (
            "connection_lost",
            1006,
            True,
        )
        events = await collect(rt.say(voice_id=VID, text="again"))
        assert events[-1].type == "task_done"
    assert engine.connections == 2


async def test_no_reconnect_when_disabled() -> None:
    engine = Engine()
    engine.on_message = closing_with(None, 1011, "upstream_closed")  # type: ignore[method-assign]
    async with running(engine) as client:
        rt = client.realtime.connect(auto_reconnect=False)
        async with rt:
            with pytest.raises(ff.RealtimeError):
                await collect(rt.say(voice_id=VID, text="x"))
            with pytest.raises(ff.RealtimeError) as info:
                await rt.context(voice_id=VID).send("y")
            assert info.value.code == "upstream_closed"
    assert engine.connections == 1


async def test_send_after_finalize_rejected() -> None:
    engine = Engine()
    async with running(engine) as client, client.realtime.connect() as rt:
        ctx = rt.context(voice_id=VID)
        with pytest.raises(ff.RealtimeError):
            await ctx.finish()  # nothing sent yet
        await ctx.send("x", finalize=True)
        with pytest.raises(ff.RealtimeError):
            await ctx.send("more")
        await collect(ctx)


def reject(status: int, body: Dict[str, Any]) -> Callable[[ServerConnection, Request], Response]:
    def process_request(conn: ServerConnection, request: Request) -> Response:
        return Response(
            status, "x", Headers({"Content-Type": "application/json"}), json.dumps(body).encode()
        )

    return process_request


@pytest.mark.parametrize(
    ("status", "body", "cls", "code"),
    [
        (429, {"error": "too_many_sessions", "limit": 4}, ff.RateLimitError, "too_many_sessions"),
        (
            401,
            {"status": 401, "error": True, "message": "Invalid or missing API key"},
            ff.AuthenticationError,
            None,
        ),
        (
            402,
            {"error": "insufficient_credits", "unit": "character", "limit": 1, "used": 1},
            ff.PaymentRequiredError,
            "insufficient_credits",
        ),
        (
            403,
            {"error": "feature_disabled", "unit": "", "limit": 0, "used": 0},
            ff.PermissionDeniedError,
            "feature_disabled",
        ),
    ],
)
async def test_handshake_rejected(
    status: int, body: Dict[str, Any], cls: type, code: Optional[str]
) -> None:
    engine = Engine()
    async with running(engine, process_request=reject(status, body)) as client:
        with pytest.raises(cls) as info:
            async with client.realtime.connect():
                pass
    assert info.value.code == code
    if status == 429:
        assert info.value.limit == 4


async def test_connection_refused() -> None:
    client = AsyncFinesse(api_key="flv_test", base_url="http://127.0.0.1:9")
    with pytest.raises(ff.APIConnectionError):
        async with client.realtime.connect():
            pass


async def test_lazy_open_and_closed_session() -> None:
    engine = Engine()
    async with running(engine) as client:
        rt = client.realtime.connect()
        assert not rt.is_open
        events = await collect(rt.say(voice_id=VID, text="lazy"))
        assert events[-1].type == "task_done"
        await rt.close()
        with pytest.raises(ff.RealtimeError) as info:
            await collect(rt.say(voice_id=VID, text="after close"))
        assert info.value.code == "session_closed"


async def test_binary_frame_without_header_is_dropped() -> None:
    engine = Engine()

    async def stray(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        if msg["type"] == "task_start":
            cid = msg["context_id"]
            await engine.audio(ws, cid, b"paired")
            await ws.send(b"stray")  # no audio_chunk header: belongs to nobody
            await ws.send(json.dumps({"type": "task_done", "context_id": cid}))
        else:
            await Engine.on_message(engine, ws, msg)

    engine.on_message = stray  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        pcm = [e.pcm async for e in rt.say(voice_id=VID, text="x") if isinstance(e, ff.AudioEvent)]
    assert pcm == [b"paired"]


async def test_malformed_frames_do_not_kill_the_session() -> None:
    engine = Engine()

    async def odd(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        if msg["type"] == "task_start":
            cid = msg["context_id"]
            await ws.send(
                json.dumps({"type": "task_started", "context_id": cid, "sample_rate": "fast"})
            )
            await ws.send("not json")
            await ws.send(json.dumps([1, 2]))
            await ws.send(
                json.dumps({"type": "audio_chunk", "context_id": cid, "chunk_index": "x"})
            )
            await ws.send(b"pcm")
            await ws.send(
                json.dumps({"type": "task_done", "context_id": cid, "num_sentences": "many"})
            )
        else:
            await Engine.on_message(engine, ws, msg)

    engine.on_message = odd  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        events = await collect(rt.say(voice_id=VID, text="x"))
        assert [e.type for e in events] == ["task_started", "audio", "task_done"]
        assert isinstance(events[1], ff.AudioEvent) and events[1].pcm == b"pcm"
        engine.on_message = lambda ws, msg: Engine.on_message(engine, ws, msg)  # type: ignore[method-assign]
        assert (await collect(rt.say(voice_id=VID, text="y")))[-1].type == "task_done"
    assert engine.connections == 1


async def test_stale_non_fatal_error_is_ignored() -> None:
    engine = Engine()

    async def stale(ws: ServerConnection, msg: Dict[str, Any]) -> None:
        await Engine.on_message(engine, ws, msg)
        if msg["type"] == "task_start":
            await ws.send(
                json.dumps(
                    {
                        "type": "error",
                        "context_id": msg["context_id"],
                        "code": "late",
                        "message": "after done",
                        "fatal": False,
                    }
                )
            )
            await ws.send(json.dumps({"type": "error", "code": "no_context", "message": "?"}))

    engine.on_message = stale  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        await collect(rt.say(voice_id=VID, text="Done."))
        ctx = rt.context(voice_id=VID)
        await ctx.send("Next.", finalize=True)
        assert (await collect(ctx))[-1].type == "task_done"
        # The stale errors must not stand in for the real reason of a later drop.
        engine.on_message = closing_with(None, 1012, "")  # type: ignore[method-assign]
        with pytest.raises(ff.RealtimeError) as info:
            await collect(rt.say(voice_id=VID, text="x"))
        assert (info.value.code, info.value.close_code) == ("connection_lost", 1012)


@pytest.mark.parametrize("code", [1001, 1012, 1013])
async def test_restart_closes_are_retryable(code: int) -> None:
    engine = Engine()
    engine.on_message = closing_with(None, code, "")  # type: ignore[method-assign]
    async with running(engine) as client, client.realtime.connect() as rt:
        with pytest.raises(ff.RealtimeError) as info:
            await collect(rt.say(voice_id=VID, text="x"))
    assert (info.value.code, info.value.close_code, info.value.retryable) == (
        "connection_lost",
        code,
        True,
    )


async def test_close_while_opening() -> None:
    engine = Engine()

    async def slow_handshake(conn: ServerConnection, request: Request) -> None:
        await asyncio.sleep(0.2)

    async with running(engine, process_request=slow_handshake) as client:
        rt = client.realtime.connect()
        opening = asyncio.ensure_future(rt.open())
        await asyncio.sleep(0.05)
        await rt.close()
        with pytest.raises(ff.RealtimeError) as info:
            await opening
        assert info.value.code == "session_closed"
        assert not rt.is_open


async def test_duplicate_context_id_rejected() -> None:
    engine = Engine()
    async with running(engine) as client, client.realtime.connect() as rt:
        first = rt.context(voice_id=VID, context_id="same")
        await first.send("hello")
        with pytest.raises(ff.RealtimeError) as info:
            await rt.context(voice_id=VID, context_id="same").send("again")
        assert info.value.code == "context_already_exists"
        await first.finish()
        assert (await collect(first))[-1].type == "task_done"


async def test_concurrent_sends_keep_start_first() -> None:
    engine = Engine()
    async with running(engine) as client:
        rt = client.realtime.connect()  # opens on the first send, while the others wait
        ctx = rt.context(voice_id=VID)
        await asyncio.gather(ctx.send("a"), ctx.send("b"), ctx.send("c"))
        await ctx.finish()
        await collect(ctx)
        await rt.close()
    kinds = [m["type"] for m in engine.received if m.get("context_id") == ctx.id]
    assert kinds == ["task_start", "task_continue", "task_continue", "task_continue"]


async def test_garbage_handshake_is_a_connection_error() -> None:
    async def garbage(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"definitely not http\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(garbage, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = AsyncFinesse(api_key="flv_test", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(ff.APIConnectionError):
            await client.realtime.connect().open()
    finally:
        server.close()


async def test_reader_survives_a_handler_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine()
    original = ff.RealtimeSession._on_message
    calls = 0

    def flaky(self: ff.RealtimeSession, frame: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unexpected")
        original(self, frame)

    monkeypatch.setattr(ff.RealtimeSession, "_on_message", flaky)
    async with running(engine) as client, client.realtime.connect() as rt:
        ctx = rt.context(voice_id=VID)
        await ctx.send("x", finalize=True)
        events = await collect(ctx)  # the first frame (task_started) is lost, the rest arrive
        assert events[-1].type == "task_done"
    assert engine.connections == 1
