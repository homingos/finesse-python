from __future__ import annotations

import httpx
import pytest

import flam_finesse as ff

from .conftest import VOICE, error


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, ff.BadRequestError),
        (401, ff.AuthenticationError),
        (402, ff.PaymentRequiredError),
        (403, ff.PermissionDeniedError),
        (404, ff.NotFoundError),
        (422, ff.APIStatusError),
    ],
)
def test_status_classes(make_client, status: int, cls: type) -> None:
    client, _ = make_client(error(status, "nope"))
    with pytest.raises(cls) as info:
        client.voices.get(VOICE["id"])
    assert info.value.status_code == status
    assert info.value.message == "nope"
    assert info.value.code is None


def test_envelope_message(make_client) -> None:
    client, _ = make_client(error(404, "Voice not found"))
    with pytest.raises(ff.NotFoundError) as info:
        client.voices.get(VOICE["id"])
    assert str(info.value) == "404: Voice not found"


def test_entitlement_denial(make_client) -> None:
    resp = httpx.Response(
        402,
        json={"error": "insufficient_credits", "unit": "character", "limit": 100, "used": 99},
        headers={"X-Billing-Remaining": "1"},
    )
    client, _ = make_client(resp)
    with pytest.raises(ff.PaymentRequiredError) as info:
        client.speech.create(voice_id=VOICE["id"], text="hi")
    e = info.value
    assert e.code == "insufficient_credits"
    assert (e.limit, e.used, e.billing_remaining) == (100, 99, 1)
    assert e.message == "insufficient credits"


def test_credit_code_in_envelope_message(make_client) -> None:
    client, _ = make_client(error(402, "insufficient_credits"))
    with pytest.raises(ff.PaymentRequiredError) as info:
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert info.value.code == "insufficient_credits"


def test_plain_message_is_not_a_code(make_client) -> None:
    client, _ = make_client(error(400, "text too long (max 10000 chars)"))
    with pytest.raises(ff.BadRequestError) as info:
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert info.value.code is None


def test_plan_limit(make_client) -> None:
    resp = httpx.Response(
        403, json={"error": "plan limit reached", "feature": "max_voices", "limit": 3}
    )
    client, _ = make_client(resp)
    with pytest.raises(ff.PermissionDeniedError) as info:
        client.voices.create(name="x")
    e = info.value
    assert (e.code, e.feature, e.limit) == ("plan_limit_reached", "max_voices", 3)


def test_feature_disabled(make_client) -> None:
    resp = httpx.Response(
        403, json={"error": "feature_disabled", "unit": "character", "limit": 0, "used": 0}
    )
    client, _ = make_client(resp)
    with pytest.raises(ff.PermissionDeniedError) as info:
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert info.value.code == "feature_disabled"


def test_non_json_body(make_client) -> None:
    client, _ = make_client(httpx.Response(418, text="I'm a teapot"))
    with pytest.raises(ff.APIStatusError) as info:
        client.voices.get(VOICE["id"])
    assert info.value.message == "I'm a teapot"
    assert info.value.body == "I'm a teapot"


def test_empty_body_uses_reason(make_client) -> None:
    client, _ = make_client(httpx.Response(401))
    with pytest.raises(ff.AuthenticationError) as info:
        client.voices.get(VOICE["id"])
    assert info.value.message == "Unauthorized"


def test_5xx_is_internal_server_error(make_client) -> None:
    client, _ = make_client(error(502, "warming up"), max_retries=0)
    with pytest.raises(ff.InternalServerError):
        client.voices.get(VOICE["id"])


def test_html_error_page_is_truncated(make_client) -> None:
    page = "<html><body>" + "Bad gateway " * 200 + "</body></html>"
    client, _ = make_client(httpx.Response(502, text=page), max_retries=0)
    with pytest.raises(ff.InternalServerError) as info:
        client.voices.get(VOICE["id"])
    assert len(info.value.message) <= 300 and info.value.message.endswith("...")
    assert info.value.body == page


def test_openai_shape_with_odd_types(make_client) -> None:
    client, _ = make_client(httpx.Response(400, json={"error": {"message": 5, "code": None}}))
    with pytest.raises(ff.BadRequestError) as info:
        client.voices.get(VOICE["id"])
    assert isinstance(info.value.message, str)


def test_credit_check_in_synthesis_keeps_its_specific_code(make_client) -> None:
    # The credit check inside synthesis sends the generic 402 code next to the specific one.
    body = {
        "status": 402,
        "error": True,
        "message": "insufficient_credits",
        "code": "payment_required",
    }
    client, _ = make_client(httpx.Response(402, json=body))
    with pytest.raises(ff.PaymentRequiredError) as info:
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert info.value.code == "insufficient_credits"
    other = {
        "status": 402,
        "error": True,
        "message": "Payment required",
        "code": "payment_required",
    }
    client, _ = make_client(httpx.Response(402, json=other))
    with pytest.raises(ff.PaymentRequiredError) as info:
        client.speech.create(voice_id=VOICE["id"], text="hi")
    assert info.value.code == "payment_required"
