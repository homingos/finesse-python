from __future__ import annotations

import base64
import json

import httpx
import pytest

import flam_finesse as ff

from .conftest import VOICE, error

VID = VOICE["id"]


def sse(*events: dict, crlf: bool = False) -> httpx.Response:
    nl = "\r\n" if crlf else "\n"
    text = "".join(f"event: {e['type']}{nl}data: {json.dumps(e)}{nl}{nl}" for e in events)
    return httpx.Response(
        200,
        content=text.encode(),
        headers={"Content-Type": "text/event-stream", "X-Billing-Remaining": "500"},
    )


def chunk(pcm: bytes, i: int) -> dict:
    return {
        "type": "chunk",
        "sample_rate": 24000,
        "audio_format": "s16le",
        "index": i,
        "audio_b64": base64.b64encode(pcm).decode(),
    }


START = {"type": "start", "sample_rate": 24000, "audio_format": "s16le"}
DONE = {
    "type": "done",
    "audio_duration_s": 1.5,
    "generation_id": "g" * 24,
    "audio_url": "https://x/a.wav",
}


def test_create(make_client) -> None:
    resp = httpx.Response(
        200,
        content=b"RIFF....",
        headers={
            "Content-Type": "audio/wav",
            "X-Sample-Rate": "24000",
            "X-Generation-Id": "g" * 24,
            "X-Audio-URL": "https://x/a.wav",
            "X-Billing-Remaining": "900",
        },
    )
    client, rec = make_client(resp)
    audio = client.speech.create(voice_id=VID, text="Hello", sample_rate=8000)
    assert rec.last.url.path.endswith(f"/voices/{VID}/speak")
    assert rec.json() == {"text": "Hello", "sample_rate": 8000}
    assert audio.data == b"RIFF...."
    assert (audio.sample_rate, audio.generation_id, audio.billing_remaining) == (
        24000,
        "g" * 24,
        900,
    )
    assert audio.audio_url == "https://x/a.wav"


def test_create_save(make_client, tmp_path) -> None:
    client, _ = make_client(
        httpx.Response(200, content=b"PCM", headers={"Content-Type": "application/octet-stream"})
    )
    audio = client.speech.create(voice_id=VID, text="x", output_format="s16le")
    audio.save(tmp_path / "a.raw")
    assert (tmp_path / "a.raw").read_bytes() == b"PCM"
    assert audio.sample_rate is None and audio.generation_id is None


@pytest.mark.parametrize("crlf", [False, True])
def test_stream(make_client, crlf: bool) -> None:
    client, rec = make_client(
        sse(START, chunk(b"\x01\x02", 0), chunk(b"\x03\x04", 1), DONE, crlf=crlf)
    )
    stream = client.speech.stream(voice_id=VID, text="Hi", num_step=16)
    assert rec.requests == []  # lazy until iterated
    chunks = list(stream)
    assert [c.pcm for c in chunks] == [b"\x01\x02", b"\x03\x04"]
    assert [c.index for c in chunks] == [0, 1]
    assert stream.sample_rate == 24000 and stream.audio_format == "s16le"
    assert stream.done is not None and stream.done.audio_duration_s == 1.5
    assert stream.done.generation_id == "g" * 24
    assert stream.billing_remaining == 500
    assert rec.last.url.path.endswith(f"/voices/{VID}/stream")
    assert rec.json() == {"text": "Hi", "num_step": 16}


def test_stream_context_manager_and_read_all(make_client) -> None:
    client, _ = make_client(sse(START, chunk(b"ab", 0), chunk(b"cd", 1), DONE))
    with client.speech.stream(voice_id=VID, text="Hi") as stream:
        assert stream.read_all() == b"abcd"


def test_stream_error_event(make_client) -> None:
    err = {"type": "error", "code": "rate_limited", "message": "engine busy"}
    client, _ = make_client(sse(START, chunk(b"ab", 0), err))
    got = []
    with pytest.raises(ff.StreamError) as info:
        for c in client.speech.stream(voice_id=VID, text="Hi"):
            got.append(c.pcm)
    assert got == [b"ab"]
    assert info.value.code == "rate_limited"


def test_stream_incomplete(make_client) -> None:
    client, _ = make_client(sse(START, chunk(b"ab", 0)))
    with pytest.raises(ff.StreamError) as info:
        list(client.speech.stream(voice_id=VID, text="Hi"))
    assert info.value.code == "stream_incomplete"


def test_stream_error_before_start(make_client) -> None:
    client, _ = make_client(error(402, "insufficient_credits"))
    with pytest.raises(ff.PaymentRequiredError):
        list(client.speech.stream(voice_id=VID, text="Hi"))


def test_stream_retries_before_start(make_client) -> None:
    client, rec = make_client(error(502, "warming"), sse(START, chunk(b"ab", 0), DONE))
    assert client.speech.stream(voice_id=VID, text="Hi").read_all() == b"ab"
    assert len(rec.requests) == 2


def test_sse_multiline_data_and_comments() -> None:
    from flam_finesse._sse import iter_sse

    lines = [": keepalive", "event: x", "data: a", "data: b", "", "data: tail"]
    assert [tuple(e) for e in iter_sse(iter(lines))] == [("x", "a\nb"), ("message", "tail")]
