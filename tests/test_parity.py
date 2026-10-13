from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

import flam_finesse as ff
from flam_finesse import _logging

from .conftest import VOICE, envelope, error
from .test_voices import page


def with_id(resp: httpx.Response, rid: str) -> httpx.Response:
    resp.headers["X-Request-Id"] = rid
    return resp


# -- with_options --------------------------------------------------------------------------


def test_with_options_copies_defaults(make_client) -> None:
    client, rec = make_client(error(503, "busy"), envelope(VOICE), envelope(VOICE))
    quick = client.with_options(max_retries=0, timeout=3.0, default_headers={"X-Team": "voice"})
    with pytest.raises(ff.InternalServerError):
        quick.voices.get(VOICE["id"])
    quick.voices.get(VOICE["id"])
    client.voices.get(VOICE["id"])
    assert len(rec.requests) == 3  # the copy did not retry the 503
    assert rec.requests[1].headers["X-Team"] == "voice"
    assert rec.requests[1].extensions["timeout"]["read"] == 3.0
    assert "X-Team" not in rec.requests[2].headers  # the original is unchanged
    assert quick._http.client is client._http.client  # one connection pool
    assert quick.base_url == client.base_url


def test_with_options_copy_does_not_close_the_pool() -> None:
    client = ff.Finesse(api_key="k", base_url="https://x.test")
    client.with_options(max_retries=1).close()
    assert not client._http.client.is_closed
    client.close()
    assert client._http.client.is_closed


async def test_async_with_options(make_async_client) -> None:
    client, rec = make_async_client(envelope(VOICE))
    await client.with_options(default_headers={"X-Team": "voice"}).voices.get(VOICE["id"])
    assert rec.last.headers["X-Team"] == "voice"
    assert client.with_options().realtime is not client.realtime


# -- with_raw_response ---------------------------------------------------------------------


def test_raw_response(make_client) -> None:
    client, _ = make_client(with_id(envelope(VOICE), "req_123"))
    raw = client.with_raw_response.voices.get(VOICE["id"])
    assert isinstance(raw, ff.RawResponse)
    assert (raw.status_code, raw.request_id, raw.headers["content-type"]) == (
        200,
        "req_123",
        "application/json",
    )
    voice = raw.parse()
    assert isinstance(voice, ff.Voice) and voice.id == VOICE["id"]


def test_raw_response_for_speech_list_and_options(make_client) -> None:
    audio = httpx.Response(200, content=b"RIFF", headers={"X-Sample-Rate": "24000"})
    client, rec = make_client(audio, page(1, [VOICE], True, 2), page(2, [VOICE], False, 2))
    raw = client.with_raw_response.speech.create(
        voice_id=VOICE["id"], text="hi", extra_headers={"X-Trace": "t"}
    )
    assert raw.headers["X-Sample-Rate"] == "24000" and raw.parse().data == b"RIFF"
    assert rec.requests[0].headers["X-Trace"] == "t"
    listed = client.with_raw_response.voices.list(page_size=1)
    assert listed.status_code == 200
    assert len(list(listed.parse())) == 2  # the page still walks the rest


def test_raw_response_has_no_stream(make_client) -> None:
    client, _ = make_client()
    with pytest.raises(ff.FinesseError, match="stream"):
        client.with_raw_response.speech.stream(voice_id=VOICE["id"], text="hi")


def test_raw_errors_still_raise(make_client) -> None:
    client, _ = make_client(error(404, "Voice not found"))
    with pytest.raises(ff.NotFoundError):
        client.with_raw_response.voices.get(VOICE["id"])


async def test_async_raw_responses_are_independent(make_async_client) -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01 if request.url.path.endswith("aaaa") else 0)
        return with_id(envelope({**VOICE, "id": request.url.path[-24:]}), request.url.path[-4:])

    a, b = "a" * 24, "b" * 24
    client, _ = make_async_client(respond, respond)
    ra, rb = await asyncio.gather(
        client.with_raw_response.voices.get(a), client.with_raw_response.voices.get(b)
    )
    assert (ra.request_id, ra.parse().id) == ("aaaa", a)
    assert (rb.request_id, rb.parse().id) == ("bbbb", b)


# -- logging -------------------------------------------------------------------------------


def test_logs_requests_and_retries_without_secrets(make_client, caplog) -> None:
    client, _ = make_client(error(503, "busy"), with_id(envelope(VOICE), "req_9"))
    with caplog.at_level(logging.DEBUG, logger="flam_finesse"):
        client.voices.get(VOICE["id"])
    text = caplog.text
    assert "GET https://api.test/flam-voice-svc/api/v1/public/voices/" in text
    assert "-> 503" in text and "-> 200" in text and "req_9" in text
    assert "retrying GET" in text and "HTTP 503" in text
    assert "flv_test" not in text


def test_finesse_log_env(monkeypatch) -> None:
    logger = logging.getLogger("flam_finesse")
    old_level, old_handlers = logger.level, list(logger.handlers)
    try:
        logger.handlers = []
        monkeypatch.setenv("FINESSE_LOG", "debug")
        _logging.setup_from_env()
        assert logger.level == logging.DEBUG and len(logger.handlers) == 1
        _logging.setup_from_env()
        assert len(logger.handlers) == 1  # not added twice
        monkeypatch.setenv("FINESSE_LOG", "nonsense")
        logger.handlers = []
        _logging.setup_from_env()
        assert logger.handlers == []
    finally:
        logger.setLevel(old_level)
        logger.handlers = old_handlers


def test_raw_response_is_the_first_request(make_client) -> None:
    job = envelope(
        {"id": "j" * 24, "org_id": "o", "status": "succeeded", "job_type": "instant_clone"}
    )
    client, rec = make_client(
        with_id(envelope({"voice": VOICE, "job_id": "j" * 24}, 202), "clone"),
        with_id(job, "poll"),
        with_id(envelope(VOICE), "get"),
    )
    raw = client.with_raw_response.voices.clone_and_wait(
        name="a", consent=True, file=b"x", poll_interval=0
    )
    assert (raw.status_code, raw.request_id) == (202, "clone")
    assert raw.parse().is_ready
    assert len(rec.requests) == 3
