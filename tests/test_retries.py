from __future__ import annotations

import httpx
import pytest

import flam_finesse as ff
from flam_finesse._base import _retry_delay

from .conftest import VOICE, envelope, error


def test_retries_then_succeeds(make_client) -> None:
    client, rec = make_client(error(503, "busy"), error(429, "slow down"), envelope(VOICE))
    assert client.voices.get(VOICE["id"]).name == "Narrator"
    assert len(rec.requests) == 3


def test_gives_up_after_max_retries(make_client) -> None:
    client, rec = make_client(error(502, "a"), error(502, "b"), error(502, "c"))
    with pytest.raises(ff.InternalServerError) as info:
        client.voices.get(VOICE["id"])
    assert info.value.message == "c"
    assert len(rec.requests) == 3


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_client_errors_not_retried(make_client, status: int) -> None:
    client, rec = make_client(error(status, "no"))
    with pytest.raises(ff.APIStatusError):
        client.voices.get(VOICE["id"])
    assert len(rec.requests) == 1


def test_connection_error_retried(make_client) -> None:
    client, rec = make_client(httpx.ConnectError("reset"), envelope(VOICE))
    client.voices.get(VOICE["id"])
    assert len(rec.requests) == 2


def test_connection_error_raised(make_client) -> None:
    client, _ = make_client(httpx.ConnectError("reset"), max_retries=0)
    with pytest.raises(ff.APIConnectionError):
        client.voices.get(VOICE["id"])


def test_timeout_raised(make_client) -> None:
    client, _ = make_client(httpx.ReadTimeout("slow"), max_retries=0)
    with pytest.raises(ff.APITimeoutError):
        client.voices.get(VOICE["id"])


def test_upload_retried_with_same_bytes(make_client, tmp_path) -> None:
    clip = tmp_path / "ref.wav"
    clip.write_bytes(b"RIFFdata")
    result = {"voice": VOICE, "job_id": "6660aa00bb11cc22dd33ee44"}
    client, rec = make_client(error(503, "busy"), envelope(result, 202))
    with clip.open("rb") as f:
        client.voices.clone(name="Ana", file=f)
    assert b"RIFFdata" in rec.requests[0].content
    assert b"RIFFdata" in rec.requests[1].content


def test_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("random.random", lambda: 0.0)
    assert _retry_delay(0, None) == 0.5
    assert _retry_delay(10, None) == 8.0
    resp = httpx.Response(429, headers={"Retry-After": "3"})
    assert _retry_delay(0, resp) == 3.0
    assert _retry_delay(0, httpx.Response(429, headers={"Retry-After": "999"})) == 0.5
    assert _retry_delay(0, httpx.Response(429, headers={"Retry-After": "soon"})) == 0.5


def test_unkeyed_post_not_retried_when_server_may_have_worked(make_client) -> None:
    # Speech streams carry no Idempotency-Key, so a failure after sending is not retried.
    client, rec = make_client(error(500, "boom"))
    with pytest.raises(ff.InternalServerError):
        client.speech.stream(voice_id=VOICE["id"], text="hi").read_all()
    assert len(rec.requests) == 1
    assert "Idempotency-Key" not in rec.requests[0].headers


def echo(resp: httpx.Response) -> object:
    """A response from a server that honours keys: it echoes the request's key."""

    def respond(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("Idempotency-Key")
        if key:
            resp.headers["Idempotency-Key"] = key
        return resp

    return respond


def test_keyed_posts_retry_with_one_key(make_client) -> None:
    ok = httpx.Response(200, content=b"RIFF")
    client, rec = make_client(echo(error(500, "boom")), httpx.ReadTimeout("slow"), ok)
    client.speech.create(voice_id=VOICE["id"], text="hi")
    keys = [r.headers.get("Idempotency-Key") for r in rec.requests]
    assert len(keys) == 3 and keys[0] and len(set(keys)) == 1
    client2, rec2 = make_client(echo(error(504, "gw")), envelope(VOICE, 201))
    client2.voices.create(name="x")
    assert len(rec2.requests) == 2
    # A new call gets a new key.
    client3, rec3 = make_client(
        httpx.Response(200, content=b"A"), httpx.Response(200, content=b"B")
    )
    client3.speech.create(voice_id=VOICE["id"], text="a")
    client3.speech.create(voice_id=VOICE["id"], text="b")
    assert (
        rec3.requests[0].headers["Idempotency-Key"] != rec3.requests[1].headers["Idempotency-Key"]
    )


def test_caller_key_is_used(make_client) -> None:
    client, rec = make_client(error(503, "busy"), envelope(VOICE, 201))
    client.voices.create(name="x", extra_headers={"Idempotency-Key": "mine-123"})
    assert [r.headers["Idempotency-Key"] for r in rec.requests] == ["mine-123", "mine-123"]


def in_progress() -> httpx.Response:
    return httpx.Response(
        409,
        json={
            "status": 409,
            "error": True,
            "code": "idempotency_in_progress",
            "message": "running",
        },
        headers={"Retry-After": "1", "X-Request-Id": "req_ip"},
    )


def test_waits_while_the_first_attempt_runs(make_client) -> None:
    # More in-progress answers than max_retries: waiting must not spend retries.
    # After three waits a 503 must still be retried.
    client, rec = make_client(
        echo(envelope(VOICE, 201)),  # an earlier call taught the client keys are honoured
        httpx.ReadTimeout("slow"),
        in_progress(),
        in_progress(),
        in_progress(),
        error(503, "busy"),
        envelope(VOICE, 201),
    )
    client.voices.create(name="first")
    client.voices.create(name="x")
    assert len(rec.requests) == 7
    rec.requests.pop(0)
    assert len({r.headers["Idempotency-Key"] for r in rec.requests}) == 1


def test_gives_up_waiting_after_the_timeout(make_client, monkeypatch) -> None:
    import flam_finesse._base as base

    clock = iter([0.0, 0.0, 50.0, 200.0, 400.0])
    monkeypatch.setattr(base.time, "monotonic", lambda: next(clock, 1000.0))
    client, _ = make_client(in_progress(), in_progress(), in_progress(), timeout=120.0)
    with pytest.raises(ff.APIStatusError) as info:
        client.voices.create(name="x")
    assert info.value.status_code == 409
    assert info.value.code == "idempotency_in_progress"
    assert info.value.request_id == "req_ip"


def test_unkeyed_409_is_not_waited_on(make_client) -> None:
    client, rec = make_client(in_progress())
    with pytest.raises(ff.APIStatusError):
        client.voices.get(VOICE["id"])
    assert len(rec.requests) == 1


def test_reused_key_error(make_client) -> None:
    body = {"status": 422, "error": True, "code": "idempotency_key_reused", "message": "different"}
    client, rec = make_client(httpx.Response(422, json=body))
    with pytest.raises(ff.APIStatusError) as info:
        client.voices.create(name="x", extra_headers={"Idempotency-Key": "k"})
    assert info.value.code == "idempotency_key_reused"
    assert len(rec.requests) == 1


def test_post_retried_when_server_did_nothing(make_client) -> None:
    ok = httpx.Response(200, content=b"RIFF")
    client, rec = make_client(httpx.ConnectError("refused"), error(429, "slow"), ok)
    client.speech.create(voice_id=VOICE["id"], text="hi")
    assert len(rec.requests) == 3
    client, rec = make_client(error(502, "warming up"), error(503, "busy"), ok)
    client.speech.create(voice_id=VOICE["id"], text="hi")
    assert len(rec.requests) == 3


def test_get_retried_on_read_timeout(make_client) -> None:
    client, rec = make_client(httpx.ReadTimeout("slow"), envelope(VOICE))
    client.voices.get(VOICE["id"])
    assert len(rec.requests) == 2


def test_409_not_retried(make_client) -> None:
    client, rec = make_client(error(409, "conflict"))
    with pytest.raises(ff.APIStatusError):
        client.voices.get(VOICE["id"])
    assert len(rec.requests) == 1


def test_timeouts() -> None:
    from flam_finesse import Finesse

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return envelope(VOICE)

    mine = httpx.Client(transport=httpx.MockTransport(handler), timeout=7.0)
    Finesse(api_key="k", base_url="https://x", http_client=mine).voices.get(VOICE["id"])
    Finesse(api_key="k", base_url="https://x", http_client=mine, timeout=3.0).voices.get(
        VOICE["id"]
    )
    assert seen[0]["read"] == 7.0
    assert seen[1]["read"] == 3.0
    own = Finesse(api_key="k", base_url="https://x")
    assert own._http.client.timeout.read == 120.0 and own._http.client.timeout.connect == 10.0


async def test_async_keyed_retry(make_async_client) -> None:
    client, rec = make_async_client(echo(error(500, "x")), in_progress(), envelope(VOICE, 201))
    await client.voices.create(name="x")
    assert len(rec.requests) == 3 and len({r.headers["Idempotency-Key"] for r in rec.requests}) == 1


def test_older_server_keeps_cautious_retries(make_client) -> None:
    # A server that does not echo keys (prod before the idempotency change) may run a
    # retried POST twice, so the SDK only retries when the server certainly did no work.
    client, rec = make_client(error(500, "boom"))
    with pytest.raises(ff.InternalServerError):
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert len(rec.requests) == 1
    client, rec = make_client(httpx.ReadTimeout("slow"))
    with pytest.raises(ff.APITimeoutError):
        client.voices.create(name="x")
    assert len(rec.requests) == 1
    client, rec = make_client(error(502, "warming"), httpx.Response(200, content=b"RIFF"))
    client.speech.create(voice_id=VOICE["id"], text="hi")
    assert len(rec.requests) == 2  # 502: the server did no work, safe either way


def test_echo_unlocks_retries_for_the_whole_client(make_client) -> None:
    client, rec = make_client(
        echo(envelope(VOICE, 201)), httpx.ReadTimeout("slow"), envelope(VOICE, 201)
    )
    client.voices.create(name="a")  # the server echoes: keys are honoured
    client.with_options(timeout=5.0).voices.create(name="b")  # copies share what was learned
    assert len(rec.requests) == 3
