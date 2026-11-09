from __future__ import annotations

import httpx
import pytest

import flam_finesse as ff

from .conftest import BASE, VOICE, Recorder, envelope

JOB_ID = "6660aa00bb11cc22dd33ee44"
TOKEN = {"token": "fin_tok_3f9c2a00112233", "expires_at": 1760000060000, "ttl_seconds": 60}


# -- consent -------------------------------------------------------------------------------


@pytest.mark.parametrize("consent", [False, None, "yes", 1])
def test_clone_without_consent_fails_before_any_request(make_client, consent) -> None:
    client, rec = make_client()
    with pytest.raises(ff.FinesseError, match="consent=True"):
        client.voices.clone(name="a", file=b"x", consent=consent)
    with pytest.raises(ff.FinesseError, match="consent=True"):
        client.voices.enroll(VOICE["id"], file=b"x", consent=consent)
    assert rec.requests == []


def test_consent_is_a_required_argument(make_client) -> None:
    client, _ = make_client()
    with pytest.raises(TypeError, match="consent"):
        client.voices.clone(name="a", file=b"x")  # type: ignore[call-arg]


def test_enroll_sends_consent(make_client) -> None:
    client, rec = make_client(envelope({"voice": VOICE, "job_id": JOB_ID}, 202))
    client.voices.enroll(VOICE["id"], file=b"x", consent=True)
    assert b'name="consent"\r\n\r\ntrue' in rec.last.content


async def test_async_clone_sends_consent(make_async_client) -> None:
    client, rec = make_async_client(envelope({"voice": VOICE, "job_id": JOB_ID}, 202))
    await client.voices.clone(name="a", file=b"x", consent=True)
    assert b'name="consent"\r\n\r\ntrue' in rec.last.content


# -- tokens --------------------------------------------------------------------------------


def test_create_token(make_client) -> None:
    client, rec = make_client(envelope(TOKEN, 201))
    tok = client.tokens.create(ttl_seconds=60, voice_ids=[VOICE["id"]])
    assert rec.last.method == "POST" and rec.last.url.path.endswith("/public/tokens")
    assert rec.json() == {"ttl_seconds": 60, "voice_ids": [VOICE["id"]]}
    assert tok.token == TOKEN["token"] and tok.ttl_seconds == 60
    assert tok.expires_at is not None and tok.expires_at.year == 2025
    assert TOKEN["token"] not in repr(tok)


def test_create_token_with_defaults_sends_an_empty_body(make_client) -> None:
    client, rec = make_client(envelope(TOKEN, 201))
    client.tokens.create()
    assert rec.json() == {}


async def test_async_create_token(make_async_client) -> None:
    client, rec = make_async_client(envelope(TOKEN, 201))
    tok = await client.tokens.create()
    assert tok.token == TOKEN["token"]


def test_raw_token_response(make_client) -> None:
    client, _ = make_client(envelope(TOKEN, 201))
    raw = client.with_raw_response.tokens.create()
    assert raw.status_code == 201 and raw.parse().token == TOKEN["token"]


def test_access_token_client_sends_bearer_not_key() -> None:
    rec = Recorder(httpx.Response(200, content=b"RIFF", headers={"Content-Type": "audio/wav"}))
    client = ff.Finesse(
        access_token="fin_tok_abc",
        base_url=BASE,
        http_client=httpx.Client(transport=httpx.MockTransport(rec)),
    )
    client.speech.create(voice_id=VOICE["id"], text="hi")
    assert rec.last.headers["Authorization"] == "Bearer fin_tok_abc"
    assert "X-API-Key" not in rec.last.headers


def test_key_and_token_together_is_an_error() -> None:
    with pytest.raises(ff.FinesseError, match="not both"):
        ff.Finesse(api_key="flv_x", access_token="fin_tok_y")


def test_token_alone_needs_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINESSE_API_KEY", raising=False)
    assert ff.Finesse(access_token="fin_tok_y").tokens is not None


# -- new error codes -----------------------------------------------------------------------


def _coded(status: int, code: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"status": status, "error": True, "message": "m", "code": code, "request_id": "req_b"},
        headers=headers,
    )


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (401, "key_expired", ff.AuthenticationError),
        (401, "token_invalid", ff.AuthenticationError),
        (403, "insufficient_scope", ff.PermissionDeniedError),
        (403, "voice_not_allowed", ff.PermissionDeniedError),
        (409, "conflict", ff.ConflictError),
        (413, "payload_too_large", ff.PayloadTooLargeError),
        (422, "invalid_input", ff.UnprocessableEntityError),
        (429, "concurrency_limited", ff.RateLimitError),
        (503, "upstream_busy", ff.InternalServerError),
    ],
)
def test_new_codes(make_client, status: int, code: str, cls: type) -> None:
    client, _ = make_client(_coded(status, code, **{"Retry-After": "2"}))
    with pytest.raises(cls) as info:
        client.voices.get(VOICE["id"], max_retries=0)
    e = info.value
    assert e.code == code and e.retry_after == 2.0
    assert e.request_id == "req_b"  # taken from the body when the header is absent


# -- direct sessions -----------------------------------------------------------------------

SESSION = {
    "session_id": "4f1c2a9e8b7d6c5b4a3f2e1d",
    "url": "wss://gpu.example/v1/stream",
    "protocol": "finesse.v1",
    "token": "ft1.abc",
    "connect_by": "2026-11-09T10:15:00Z",
    "ttl_seconds": 3600,
    "budget_chars": 5000,
    "region": "ap-south",
    "voices": [VOICE["id"]],
}


def test_create_direct_session(make_client) -> None:
    client, rec = make_client(envelope(SESSION, 201))
    s = client.realtime.sessions.create(voice_ids=[VOICE["id"]], budget_chars=5000)
    assert rec.last.method == "POST"
    assert rec.last.url.path == "/flam-voice-svc/api/v1/public/realtime/sessions"
    assert rec.json() == {"voice_ids": [VOICE["id"]], "budget_chars": 5000}
    assert (s.token, s.region, s.budget_chars) == ("ft1.abc", "ap-south", 5000)
    assert s.connect_by is not None and s.connect_by.year == 2026


async def test_async_create_direct_session(make_async_client) -> None:
    client, rec = make_async_client(envelope(SESSION, 201))
    s = await client.realtime.sessions.create(voice_ids=[VOICE["id"]])
    assert rec.json() == {"voice_ids": [VOICE["id"]]}
    assert s.url == SESSION["url"]
