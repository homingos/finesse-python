"""Contract tests against a real Finesse API. Skipped unless FINESSE_LIVE=1.

    FINESSE_LIVE=1 FINESSE_API_KEY=flv_... \
    FINESSE_BASE_URL=https://finesse.example.test uv run pytest tests/live -v

They synthesize a few short sentences (billed per character) and open realtime sessions. They
refuse to run against production unless FINESSE_LIVE_ALLOW_PROD=1.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import flam_finesse as ff
from flam_finesse._base import DEFAULT_BASE_URL

pytestmark = pytest.mark.live

if os.environ.get("FINESSE_LIVE") != "1":
    pytest.skip("live tests need FINESSE_LIVE=1", allow_module_level=True)
if (os.environ.get("FINESSE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/") == DEFAULT_BASE_URL and (
    os.environ.get("FINESSE_LIVE_ALLOW_PROD") != "1"
):
    pytest.skip("refusing to run live tests against production", allow_module_level=True)


@pytest.fixture(scope="module")
def client() -> ff.Finesse:
    return ff.Finesse()


@pytest.fixture(scope="module")
def voice(client: ff.Finesse) -> ff.Voice:
    for v in client.voices.list(page_size=100):
        if v.is_ready:
            return v
    pytest.skip("no ready voice visible to this key")


def test_list_and_get(client: ff.Finesse, voice: ff.Voice) -> None:
    page = client.voices.list(page_size=1)
    assert page.page == 1 and page.total >= 1
    got = client.voices.get(voice.id)
    assert got.id == voice.id and got.created_at is not None


def test_not_found(client: ff.Finesse) -> None:
    with pytest.raises(ff.NotFoundError):
        client.voices.get("000000000000000000000000")


def test_bad_key() -> None:
    with pytest.raises(ff.AuthenticationError):
        ff.Finesse(api_key="flv_invalid").voices.list()


def test_speech_create(client: ff.Finesse, voice: ff.Voice) -> None:
    audio = client.speech.create(voice_id=voice.id, text="Live check.")
    assert audio.data[:4] == b"RIFF"
    assert audio.sample_rate == 24000


def test_speech_create_pcm_low_rate(client: ff.Finesse, voice: ff.Voice) -> None:
    audio = client.speech.create(
        voice_id=voice.id, text="Raw audio.", output_format="s16le", sample_rate=8000
    )
    assert audio.sample_rate == 24000  # the engine never goes below its native rate
    assert len(audio.data) > 0 and len(audio.data) % 2 == 0


def test_speech_stream(client: ff.Finesse, voice: ff.Voice) -> None:
    with client.speech.stream(voice_id=voice.id, text="Streaming check.") as stream:
        pcm = stream.read_all()
    assert len(pcm) > 0
    assert stream.done is not None and (stream.done.audio_duration_s or 0) > 0


async def test_realtime_say(voice: ff.Voice) -> None:
    client = ff.AsyncFinesse()
    async with client.realtime.connect() as rt:
        events = [e async for e in rt.say(voice_id=voice.id, text="Realtime check.")]
    kinds = [e.type for e in events]
    assert kinds[0] == "task_started" and kinds[-1] == "task_done"
    assert any(isinstance(e, ff.AudioEvent) and e.pcm for e in events)


async def test_realtime_cancel(voice: ff.Voice) -> None:
    client = ff.AsyncFinesse()
    async with client.realtime.connect() as rt:
        ctx = rt.context(voice_id=voice.id)
        await ctx.send("This is a long sentence that we will interrupt part of the way through.")
        last = None
        async for event in ctx:
            last = event
            if event.type == "audio":
                await ctx.cancel()
        assert last is not None and last.type in ("task_cancelled", "task_done")


async def test_realtime_session_cap() -> None:
    # Each session holds one of the plan's concurrency slots (2 on free); the service also caps
    # a workspace at 4 sessions per pod. Whichever comes first refuses the next handshake.
    client = ff.AsyncFinesse()
    sessions: list = []
    try:
        with pytest.raises(ff.RateLimitError) as info:
            for _ in range(5):
                s = client.realtime.connect()
                await s.open()
                sessions.append(s)
        assert info.value.code in ("concurrency_limited", "too_many_sessions")
        assert sessions, "at least one session should open"
    finally:
        await asyncio.gather(*(s.close() for s in sessions), return_exceptions=True)


def test_clone_requires_consent_locally(client: ff.Finesse) -> None:
    with pytest.raises(ff.FinesseError, match="consent=True"):
        client.voices.clone(name="never sent", file=b"x", consent=False)


def test_token_speech_then_reuse_is_refused(client: ff.Finesse, voice: ff.Voice) -> None:
    token = client.tokens.create(ttl_seconds=60, voice_ids=[voice.id])
    with ff.Finesse(access_token=token.token) as tc:
        audio = tc.speech.create(voice_id=voice.id, text="Token check.")
        assert audio.data[:4] == b"RIFF"
        with pytest.raises(ff.AuthenticationError) as info:
            tc.speech.create(voice_id=voice.id, text="Again.")
    assert info.value.code == "token_invalid"


async def test_realtime_with_a_token(client: ff.Finesse, voice: ff.Voice) -> None:
    def fresh() -> str:
        return client.tokens.create(voice_ids=[voice.id]).token

    rt_client = ff.AsyncFinesse(access_token=fresh())
    async with rt_client.realtime.connect(get_access_token=fresh) as rt:
        events = [e async for e in rt.say(voice_id=voice.id, text="Token realtime.")]
    assert events[-1].type == "task_done"
