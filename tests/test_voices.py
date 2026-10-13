from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

import flam_finesse as ff

from .conftest import VOICE, envelope

JOB_ID = "6660aa00bb11cc22dd33ee44"


def page(n: int, voices: list, has_more: bool, total: int = 3) -> httpx.Response:
    return envelope(
        {"voices": voices, "page": n, "page_size": 2, "total": total, "has_more": has_more}
    )


def job(status: str, **extra: object) -> httpx.Response:
    return envelope(
        {
            "id": JOB_ID,
            "org_id": VOICE["org_id"],
            "voice_id": VOICE["id"],
            "job_type": "instant_clone",
            "status": status,
            "progress": 100 if status == "succeeded" else 0,
            "attempts": 0,
            "created_at": 1760000000000,
            **extra,
        }
    )


def test_voice_model() -> None:
    v = ff.Voice.model_validate({**VOICE, "brand_new_field": 1})
    assert v.labels == {"gender": "female"}
    assert v.created_at == datetime(2025, 10, 9, 8, 53, 20, tzinfo=timezone.utc)
    assert v.is_ready
    assert v.model_extra == {"brand_new_field": 1}


def test_list_walks_pages(make_client) -> None:
    a = {**VOICE, "id": "a" * 24}
    b = {**VOICE, "id": "b" * 24}
    c = {**VOICE, "id": "c" * 24}
    client, rec = make_client(page(1, [a, b], True), page(2, [c], False))
    first = client.voices.list(page_size=2)
    assert (first.total, first.has_more, len(first.voices)) == (3, True, 2)
    assert len(rec.requests) == 1
    assert [v.id for v in first] == ["a" * 24, "b" * 24, "c" * 24]
    assert [dict(r.url.params) for r in rec.requests] == [
        {"page": "1", "page_size": "2"},
        {"page": "2", "page_size": "2"},
    ]


def test_list_stops_on_empty_page(make_client) -> None:
    client, rec = make_client(page(1, [], True))
    assert list(client.voices.list()) == []
    assert len(rec.requests) == 1


def test_create(make_client) -> None:
    client, rec = make_client(envelope({**VOICE, "status": "draft"}, 201))
    v = client.voices.create(name="Narrator", language="en", labels={"gender": "female"})
    assert v.status == "draft"
    assert rec.json() == {"name": "Narrator", "language": "en", "labels": {"gender": "female"}}


def test_update_sends_only_given_fields(make_client) -> None:
    client, rec = make_client(envelope(VOICE))
    client.voices.update(VOICE["id"], description="", labels={})
    assert rec.last.method == "PATCH"
    assert rec.json() == {"description": "", "labels": {}}


def test_delete(make_client) -> None:
    client, rec = make_client(envelope({"deleted": True}))
    assert client.voices.delete(VOICE["id"]) is None
    assert rec.last.method == "DELETE"


def test_clone_multipart(make_client) -> None:
    client, rec = make_client(
        envelope({"voice": {**VOICE, "status": "processing"}, "job_id": JOB_ID}, 202)
    )
    res = client.voices.clone(
        name="Ana",
        file=("ref.wav", b"RIFFbytes"),
        labels={"accent": "british"},
        ref_text="hello",
        remove_background_noise=True,
    )
    assert res.job_id == JOB_ID and res.voice.status == "processing"
    req = rec.last
    assert req.url.path.endswith("/voices/clone")
    body = req.content
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    for part in (
        b'name="name"\r\n\r\nAna',
        b'name="ref_text"\r\n\r\nhello',
        b'name="remove_background_noise"\r\n\r\ntrue',
        b'filename="ref.wav"',
        b"Content-Type: audio/x-wav",
        b"RIFFbytes",
    ):
        assert part in body, part
    assert json.dumps({"accent": "british"}).encode() in body


def test_clone_from_path_and_bytes(make_client, tmp_path) -> None:
    clip = tmp_path / "me.mp3"
    clip.write_bytes(b"ID3x")
    ok = envelope({"voice": VOICE, "job_id": JOB_ID}, 202)
    client, rec = make_client(ok, ok)
    client.voices.clone(name="a", file=clip)
    assert b'filename="me.mp3"' in rec.requests[0].content
    assert b"Content-Type: audio/mpeg" in rec.requests[0].content
    client.voices.clone(name="b", file=b"RAWCLIP")
    assert b'filename="audio.wav"' in rec.requests[1].content
    assert b"RAWCLIP" in rec.requests[1].content


def test_text_mode_file_rejected(make_client, tmp_path) -> None:
    clip = tmp_path / "a.wav"
    clip.write_text("x")
    client, _ = make_client()
    with clip.open("r") as f, pytest.raises(TypeError, match="binary"):
        client.voices.clone(name="a", file=f)  # type: ignore[arg-type]


def test_enroll(make_client) -> None:
    client, rec = make_client(envelope({"voice": VOICE, "job_id": JOB_ID}, 202))
    client.voices.enroll(VOICE["id"], file=b"x", language="hi")
    assert rec.last.url.path.endswith(f"/voices/{VOICE['id']}/enroll")
    assert b'name="language"\r\n\r\nhi' in rec.last.content


def test_clone_and_wait(make_client) -> None:
    seen: list = []
    client, rec = make_client(
        envelope({"voice": {**VOICE, "status": "processing"}, "job_id": JOB_ID}, 202),
        job("running"),
        job("succeeded"),
        envelope(VOICE),
    )
    v = client.voices.clone_and_wait(
        name="Ana", file=b"x", poll_interval=0, on_update=lambda j: seen.append(j.status)
    )
    assert v.is_ready
    assert seen == ["running", "succeeded"]
    assert [r.url.path.rsplit("/", 2)[-2] for r in rec.requests[1:3]] == ["jobs", "jobs"]


def test_wait_failed(make_client) -> None:
    client, _ = make_client(job("failed", error={"message": "clip too short", "status_code": 400}))
    with pytest.raises(ff.CloneFailedError, match="clip too short") as info:
        client.jobs.wait(JOB_ID, poll_interval=0)
    assert info.value.job.status == "failed"


def test_wait_timeout(make_client) -> None:
    client, _ = make_client(job("running"))
    with pytest.raises(ff.CloneTimeoutError):
        client.jobs.wait(JOB_ID, timeout=0, poll_interval=0)
