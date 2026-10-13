from __future__ import annotations

import json
from typing import Any, Callable, List

import httpx
import pytest

import flam_finesse._base as base

BASE = "https://api.test"
PREFIX = "/flam-voice-svc/api/v1/public"

VOICE = {
    "id": "665f1a2b3c4d5e6f7a8b9c0d",
    "org_id": "665f00000000000000000001",
    "name": "Narrator",
    "clone_method": "instant",
    "model": "finesse-v1",
    "status": "ready",
    "visibility": "private",
    "primary_language": "en",
    "labels": [{"key": "gender", "value": "female"}],
    "created_at": 1760000000000,
    "updated_at": 1760000300000,
    "language": "en",
    "gender": "female",
}


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"status": status, "error": False, "data": data})


def error(status: int, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, json={"status": status, "error": True, "message": message}, headers=headers
    )


class Recorder:
    """A mock transport that answers with queued responses and records requests."""

    def __init__(self, *responses: Any) -> None:
        self.responses: List[Any] = list(responses)
        self.requests: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if not self.responses:
            raise AssertionError(f"unexpected request {request.method} {request.url}")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if callable(nxt):
            return nxt(request)
        return nxt

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def json(self, i: int = -1) -> Any:
        return json.loads(self.requests[i].content)


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base, "_retry_delay", lambda attempt, response: 0)


@pytest.fixture
def make_client() -> Callable[..., Any]:
    from flam_finesse import Finesse

    def make(*responses: Any, **kwargs: Any) -> tuple[Finesse, Recorder]:
        rec = Recorder(*responses)
        client = Finesse(
            api_key="flv_test",
            base_url=BASE,
            http_client=httpx.Client(transport=httpx.MockTransport(rec)),
            **kwargs,
        )
        return client, rec

    return make


@pytest.fixture
def make_async_client() -> Callable[..., Any]:
    from flam_finesse import AsyncFinesse

    def make(*responses: Any, **kwargs: Any) -> tuple[AsyncFinesse, Recorder]:
        rec = Recorder(*responses)
        client = AsyncFinesse(
            api_key="flv_test",
            base_url=BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(rec)),
            **kwargs,
        )
        return client, rec

    return make
