from __future__ import annotations

import httpx
import pytest

import flam_finesse as ff

from .conftest import VOICE, envelope, error
from .test_speech import DONE, START, chunk, sse
from .test_voices import page


def test_per_request_headers_do_not_leak(make_client) -> None:
    client, rec = make_client(envelope(VOICE), envelope(VOICE))
    client.voices.get(VOICE["id"], extra_headers={"X-Trace": "abc", "X-Finesse-Client": "app/1"})
    client.voices.get(VOICE["id"])
    assert rec.requests[0].headers["X-Trace"] == "abc"
    assert rec.requests[0].headers["X-Finesse-Client"] == "app/1"
    assert "X-Trace" not in rec.requests[1].headers
    assert rec.requests[1].headers["X-Finesse-Client"].startswith("flam-finesse-python/")


def test_per_request_max_retries(make_client) -> None:
    client, rec = make_client(error(503, "busy"), error(503, "busy"))
    with pytest.raises(ff.InternalServerError):
        client.voices.get(VOICE["id"], max_retries=0)
    assert len(rec.requests) == 1
    # The client default (2 retries) still applies to calls without an override.
    client2, rec2 = make_client(error(503, "a"), error(503, "b"), envelope(VOICE))
    client2.voices.get(VOICE["id"])
    assert len(rec2.requests) == 3


def test_per_request_timeout(make_client) -> None:
    client, rec = make_client(envelope(VOICE), envelope(VOICE))
    client.voices.get(VOICE["id"], timeout=3.0)
    client.voices.get(VOICE["id"])
    assert rec.requests[0].extensions["timeout"]["read"] == 3.0
    # Without an override the passed-in httpx client keeps its own default (5 s).
    assert rec.requests[1].extensions["timeout"]["read"] == 5.0


def test_options_reach_every_kind_of_call(make_client) -> None:
    result = {"voice": VOICE, "job_id": "j" * 24}
    job = envelope(
        {"id": "j" * 24, "org_id": "o", "status": "running", "job_type": "instant_clone"}
    )
    client, rec = make_client(
        page(1, [VOICE], False, 1),
        envelope(VOICE, 201),
        envelope(VOICE),
        envelope({"deleted": True}),
        envelope(result, 202),
        envelope(result, 202),
        job,
        httpx.Response(200, content=b"RIFF"),
        sse(START, chunk(b"ab", 0), DONE),
    )
    h = {"X-Trace": "t"}
    list(client.voices.list(extra_headers=h))
    client.voices.create(name="n", extra_headers=h)
    client.voices.update(VOICE["id"], name="m", extra_headers=h)
    client.voices.delete(VOICE["id"], extra_headers=h)
    client.voices.clone(name="a", file=b"x", extra_headers=h)
    client.voices.enroll(VOICE["id"], file=b"x", extra_headers=h)
    client.jobs.get("j" * 24, extra_headers=h)
    client.speech.create(voice_id=VOICE["id"], text="hi", extra_headers=h)
    client.speech.stream(voice_id=VOICE["id"], text="hi", extra_headers=h).read_all()
    assert all(r.headers.get("X-Trace") == "t" for r in rec.requests)
    assert len(rec.requests) == 9


async def test_async_options_and_direct_iteration(make_async_client) -> None:
    a, b = {**VOICE, "id": "a" * 24}, {**VOICE, "id": "b" * 24}
    client, rec = make_async_client(
        page(1, [a], True, 2), page(2, [b], False, 2), page(1, [a], True, 2), envelope(VOICE)
    )
    ids = [v.id async for v in client.voices.list(page_size=1, extra_headers={"X-Trace": "t"})]
    assert ids == ["a" * 24, "b" * 24]
    first = await client.voices.list(page_size=1)
    assert isinstance(first, ff.AsyncVoicePage) and first.has_more
    await client.voices.get(VOICE["id"], timeout=7.0)
    assert rec.requests[0].headers["X-Trace"] == "t" and rec.requests[1].headers["X-Trace"] == "t"
    assert "X-Trace" not in rec.requests[2].headers
    assert rec.requests[3].extensions["timeout"]["read"] == 7.0
