from __future__ import annotations

import pytest

import flam_finesse as ff

from .conftest import VOICE, envelope, error
from .test_speech import DONE, START, chunk, sse
from .test_voices import JOB_ID, job, page


async def test_list_walks_pages(make_async_client) -> None:
    a, b = {**VOICE, "id": "a" * 24}, {**VOICE, "id": "b" * 24}
    client, rec = make_async_client(page(1, [a], True, 2), page(2, [b], False, 2))
    first = await client.voices.list(page_size=1)
    assert [v.id async for v in first] == ["a" * 24, "b" * 24]
    assert len(rec.requests) == 2


async def test_crud(make_async_client) -> None:
    client, rec = make_async_client(
        envelope(VOICE), envelope(VOICE, 201), envelope(VOICE), envelope({"deleted": True})
    )
    assert (await client.voices.get(VOICE["id"])).labels == {"gender": "female"}
    await client.voices.create(name="n")
    await client.voices.update(VOICE["id"], name="m")
    await client.voices.delete(VOICE["id"])
    assert [r.method for r in rec.requests] == ["GET", "POST", "PATCH", "DELETE"]


async def test_clone_and_wait(make_async_client) -> None:
    client, _ = make_async_client(
        envelope({"voice": VOICE, "job_id": JOB_ID}, 202),
        job("running"),
        job("succeeded"),
        envelope(VOICE),
    )
    v = await client.voices.clone_and_wait(name="a", consent=True, file=b"x", poll_interval=0)
    assert v.is_ready


async def test_wait_failed(make_async_client) -> None:
    client, _ = make_async_client(job("expired"))
    with pytest.raises(ff.CloneFailedError):
        await client.jobs.wait(JOB_ID, poll_interval=0)


async def test_speech_create(make_async_client) -> None:
    import httpx

    client, _ = make_async_client(
        httpx.Response(200, content=b"RIFF", headers={"X-Sample-Rate": "24000"})
    )
    audio = await client.speech.create(voice_id=VOICE["id"], text="hi")
    assert audio.data == b"RIFF" and audio.sample_rate == 24000


async def test_stream(make_async_client) -> None:
    client, _ = make_async_client(sse(START, chunk(b"ab", 0), chunk(b"cd", 1), DONE))
    async with client.speech.stream(voice_id=VOICE["id"], text="hi") as stream:
        pcm = [c.pcm async for c in stream]
    assert pcm == [b"ab", b"cd"]
    assert stream.done is not None


async def test_stream_read_all_and_incomplete(make_async_client) -> None:
    client, _ = make_async_client(sse(START, chunk(b"ab", 0), DONE), sse(START))
    assert await client.speech.stream(voice_id=VOICE["id"], text="hi").read_all() == b"ab"
    with pytest.raises(ff.StreamError):
        await client.speech.stream(voice_id=VOICE["id"], text="hi").read_all()


async def test_retries(make_async_client) -> None:
    import httpx

    client, rec = make_async_client(httpx.ConnectError("x"), error(503, "busy"), envelope(VOICE))
    await client.voices.get(VOICE["id"])
    assert len(rec.requests) == 3


async def test_errors(make_async_client) -> None:
    client, _ = make_async_client(error(401, "Invalid or missing API key"))
    with pytest.raises(ff.AuthenticationError):
        await client.voices.get(VOICE["id"])


async def test_context_manager(make_async_client) -> None:
    client, _ = make_async_client()
    async with client:
        pass
