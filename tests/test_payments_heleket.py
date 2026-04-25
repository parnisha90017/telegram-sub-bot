"""Unit tests for Heleket sign + create_invoice + verify_webhook.
Ported from sid-bot/tests/test_payments_heleket.py with two adjustments:
  * callback_url moved from create_invoice() args to HeleketProvider ctor;
  * verify_webhook now returns (valid, event) tuple, not Optional[event].
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest
from aioresponses import aioresponses

from app.payments.heleket import (
    HELEKET_API_URL,
    HeleketProvider,
    _canonical_json,
    _sign,
)


MERCHANT = "mmm-uuid-1111"
API_KEY = "api-key-xyz"
CALLBACK = "https://host/heleket/webhook"


def _reference_sign(body: dict, api_key: str) -> str:
    """Re-implements the algorithm inline so the test validates the library
    function (HeleketProvider._sign) against the documented formula, not
    against itself."""
    body_json = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    body_json = body_json.replace("/", "\\/")
    body_b64 = base64.b64encode(body_json.encode("utf-8")).decode("ascii")
    return hashlib.md5((body_b64 + api_key).encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# sign algorithm
# -----------------------------------------------------------------------------

def test_sign_escapes_forward_slash_like_php_json_encode():
    """Regression: PHP's json_encode backslash-escapes `/` by default; Python's
    json.dumps does not. The sign must match PHP's output, otherwise Heleket
    rejects our create_invoice calls (and our webhook verification never
    matches what they sent)."""
    body = {"url_callback": "https://example.com/webhook"}
    canonical = _canonical_json(body).decode("utf-8")
    assert canonical == '{"url_callback":"https:\\/\\/example.com\\/webhook"}'


def test_sign_generation_matches_documented_formula():
    body = {
        "amount": "17.00",
        "currency": "USDT",
        "network": "tron",
        "order_id": "42",
        "url_callback": "https://sub-bot.example/heleket/webhook",
        "lifetime": 3600,
    }
    assert _sign(body, API_KEY) == _reference_sign(body, API_KEY)


# -----------------------------------------------------------------------------
# create_invoice
# -----------------------------------------------------------------------------

async def test_create_invoice_sends_usdt_tron():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    captured: dict = {}

    with aioresponses() as mocked:
        from aioresponses.core import CallbackResult

        def _cb(url, **kwargs):
            captured["headers"] = kwargs.get("headers") or {}
            captured["body"] = kwargs.get("json") or json.loads(kwargs.get("data") or b"{}")
            return CallbackResult(
                status=200,
                payload={
                    "state": 0,
                    "result": {"uuid": "pay-uuid-1", "url": "https://pay.heleket/abc"},
                },
            )

        mocked.post(HELEKET_API_URL, callback=_cb)
        invoice = await provider.create_invoice(
            amount_usd=17.0, order_id="1", description="t",
        )

    assert invoice.provider == "heleket"
    assert invoice.invoice_id == "pay-uuid-1"
    assert invoice.pay_url == "https://pay.heleket/abc"
    assert captured["headers"]["merchant"] == MERCHANT
    body = captured["body"]
    assert body == {
        "amount": "17.00",
        "currency": "USDT",
        "network": "tron",
        "order_id": "1",
        "url_callback": CALLBACK,
        "lifetime": 3600,
    }
    assert captured["headers"]["sign"] == _reference_sign(body, API_KEY)


async def test_create_invoice_sends_body_matching_sign():
    """Prod regression from sid-bot: the body aiohttp actually transmits must
    be the SAME byte stream we hashed to produce the `sign` header. Previously
    `json=body` made aiohttp re-serialize with different separators/escape, so
    Heleket computed a different MD5 over the received bytes and rejected with
    'Invalid Sign'."""
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    captured: dict = {}

    with aioresponses() as mocked:
        from aioresponses.core import CallbackResult

        def _cb(url, **kwargs):
            captured["body_bytes"] = kwargs.get("data")
            captured["headers"] = kwargs.get("headers") or {}
            return CallbackResult(
                status=200,
                payload={"state": 0, "result": {"uuid": "u", "url": "https://pay/x"}},
            )

        mocked.post(HELEKET_API_URL, callback=_cb)
        await provider.create_invoice(amount_usd=17.0, order_id="42")

    body_bytes = captured["body_bytes"]
    assert isinstance(body_bytes, (bytes, bytearray)), f"got {type(body_bytes)}"
    sent_sign = captured["headers"]["sign"]
    recomputed = hashlib.md5(
        (base64.b64encode(bytes(body_bytes)).decode("ascii") + API_KEY).encode("utf-8")
    ).hexdigest()
    assert recomputed == sent_sign
    assert b'"url_callback":"https:\\/\\/host\\/heleket\\/webhook"' in bytes(body_bytes)


async def test_create_invoice_parses_response():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    with aioresponses() as mocked:
        mocked.post(
            HELEKET_API_URL,
            status=200,
            payload={
                "state": 0,
                "result": {
                    "uuid": "8b03432e-385b-4670-8d06-064591096795",
                    "url": "https://pay.heleket.com/pay/xyz",
                    "order_id": "99",
                    "amount": "17.00",
                    "currency": "USDT",
                    "network": "tron",
                },
            },
        )
        invoice = await provider.create_invoice(amount_usd=17.0, order_id="99")

    assert invoice.invoice_id == "8b03432e-385b-4670-8d06-064591096795"
    assert invoice.pay_url == "https://pay.heleket.com/pay/xyz"


async def test_create_invoice_raises_on_error_state():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    with aioresponses() as mocked:
        mocked.post(
            HELEKET_API_URL,
            status=200,
            payload={"state": 1, "message": "validation failed"},
        )
        with pytest.raises(RuntimeError):
            await provider.create_invoice(amount_usd=17.0, order_id="1")


async def test_create_invoice_raises_on_malformed_ok_response():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    with aioresponses() as mocked:
        mocked.post(
            HELEKET_API_URL,
            status=200,
            payload={"state": 0, "result": {"uuid": "x"}},  # no url
        )
        with pytest.raises(RuntimeError):
            await provider.create_invoice(amount_usd=17.0, order_id="1")


# -----------------------------------------------------------------------------
# verify_webhook (returns tuple[bool, Optional[WebhookEvent]])
# -----------------------------------------------------------------------------

def test_verify_webhook_valid_signature_returns_event():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    payload = {
        "type": "payment",
        "uuid": "pay-uuid-2",
        "order_id": "42",
        "amount": "17.00",
        "currency": "USDT",
        "network": "tron",
        "status": "paid",
    }
    payload_with_sign = {**payload, "sign": _reference_sign(payload, API_KEY)}
    body = json.dumps(payload_with_sign, ensure_ascii=False, separators=(",", ":")).encode()

    valid, event = provider.verify_webhook(body, headers={})
    assert valid is True
    assert event is not None
    assert event.provider == "heleket"
    assert event.invoice_id == "pay-uuid-2"
    assert event.status == "paid"
    assert event.amount_usd == 17.0


def test_verify_webhook_paid_over_normalized_to_paid():
    """Overpayment must trigger the same mark_paid path as 'paid'."""
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    payload = {"uuid": "pay-3", "order_id": "7", "amount": "20.00", "status": "paid_over"}
    payload["sign"] = _reference_sign(
        {k: v for k, v in payload.items() if k != "sign"}, API_KEY,
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    valid, event = provider.verify_webhook(body, headers={})
    assert valid is True
    assert event is not None
    assert event.status == "paid"
    assert event.amount_usd == 20.0


def test_verify_webhook_invalid_signature_returns_invalid():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    payload = {"uuid": "x", "order_id": "1", "status": "paid", "sign": "deadbeef"}
    body = json.dumps(payload, separators=(",", ":")).encode()
    valid, event = provider.verify_webhook(body, headers={})
    assert valid is False
    assert event is None


def test_verify_webhook_missing_sign_returns_invalid():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    payload = {"uuid": "x", "order_id": "1", "status": "paid"}
    body = json.dumps(payload, separators=(",", ":")).encode()
    valid, event = provider.verify_webhook(body, headers={})
    assert valid is False
    assert event is None


def test_verify_webhook_malformed_json_returns_invalid():
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    valid_a, ev_a = provider.verify_webhook(b"not json", headers={})
    valid_b, ev_b = provider.verify_webhook(b'["list","not","dict"]', headers={})
    assert valid_a is False and ev_a is None
    assert valid_b is False and ev_b is None


def test_verify_webhook_fail_status_not_normalized():
    """'fail'/'cancel'/etc. must pass through as-is so the generic handler
    sees a non-'paid' status and ignores the event."""
    provider = HeleketProvider(MERCHANT, API_KEY, callback_url=CALLBACK)
    for raw_status in ("fail", "cancel", "system_fail", "wrong_amount"):
        payload = {"uuid": "x", "order_id": "1", "status": raw_status}
        payload["sign"] = _reference_sign(
            {k: v for k, v in payload.items() if k != "sign"}, API_KEY,
        )
        body = json.dumps(payload, separators=(",", ":")).encode()
        valid, event = provider.verify_webhook(body, headers={})
        assert valid is True
        assert event is not None
        assert event.status == raw_status
