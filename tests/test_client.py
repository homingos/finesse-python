from __future__ import annotations

import pytest

from flam_finesse import Finesse, FinesseError, __version__
from flam_finesse._base import DEFAULT_BASE_URL

from .conftest import PREFIX, VOICE, envelope


def test_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FINESSE_API_KEY", raising=False)
    with pytest.raises(FinesseError, match="FINESSE_API_KEY"):
        Finesse()


def test_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINESSE_API_KEY", "flv_env")
    monkeypatch.delenv("FINESSE_BASE_URL", raising=False)
    client = Finesse()
    assert client.base_url == DEFAULT_BASE_URL
    assert client._config.api_key == "flv_env"
    monkeypatch.setenv("FINESSE_BASE_URL", "https://finesse.example.test/")
    assert Finesse().base_url == "https://finesse.example.test"


def test_headers_and_url(make_client) -> None:
    client, rec = make_client(envelope(VOICE))
    client.voices.get(VOICE["id"])
    req = rec.last
    assert str(req.url) == f"https://api.test{PREFIX}/voices/{VOICE['id']}"
    assert req.headers["X-API-Key"] == "flv_test"
    assert req.headers["X-Finesse-Client"] == f"flam-finesse-python/{__version__}"
    assert req.headers["User-Agent"].startswith(f"flam-finesse-python/{__version__} python/")


def test_default_headers_override(make_client) -> None:
    client, rec = make_client(
        envelope(VOICE), default_headers={"X-Finesse-Client": "pipecat-finesse/0.1.0"}
    )
    client.voices.get(VOICE["id"])
    assert rec.last.headers["X-Finesse-Client"] == "pipecat-finesse/0.1.0"


def test_ids_are_escaped(make_client) -> None:
    client, rec = make_client(envelope(VOICE))
    client.voices.get("a/../b")
    assert rec.last.url.raw_path.decode().endswith("/voices/a%2F..%2Fb")
    with pytest.raises(ValueError):
        client.voices.get("..")


def test_ws_base_url() -> None:
    c = Finesse(api_key="k", base_url="https://h.example")
    assert c._config.ws_base_url == "wss://h.example"
    assert Finesse(api_key="k", base_url="http://localhost:8080")._config.ws_base_url == (
        "ws://localhost:8080"
    )
