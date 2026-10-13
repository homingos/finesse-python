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


def test_post_not_retried_when_server_may_have_worked(make_client) -> None:
    client, rec = make_client(error(500, "boom"))
    with pytest.raises(ff.InternalServerError):
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert len(rec.requests) == 1
    client, rec = make_client(httpx.ReadTimeout("slow"))
    with pytest.raises(ff.APITimeoutError):
        client.voices.create(name="x")
    assert len(rec.requests) == 1
    client, rec = make_client(error(504, "gateway"))
    with pytest.raises(ff.InternalServerError):
        client.speech.create(voice_id=VOICE["id"], text="hi")
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
