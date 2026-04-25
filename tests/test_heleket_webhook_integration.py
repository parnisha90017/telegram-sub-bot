"""End-to-end Heleket webhook through aiohttp test server.

Pipeline being tested: POST /h → HeleketProvider.verify_webhook (real) →
process_paid_invoice (mocked) → unban_from_all_chats + issue_invite_links_and_send
(both mocked). Validates the regression-prone integration boundaries:
sign verification, status routing, idempotency, error handling.
"""
from __future__ import annotations

import base64
import hashlib
import json
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.payments.heleket import HeleketProvider
from app.web.heleket_webhook import make_heleket_handler


MERCHANT = "mmm-uuid-2222"
API_KEY = "api-key-heleket"


def _sign_body(body: dict) -> str:
    s = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("/", "\\/")
    b64 = base64.b64encode(s.encode("utf-8")).decode("ascii")
    return hashlib.md5((b64 + API_KEY).encode("utf-8")).hexdigest()


def _signed_body(payload: dict) -> bytes:
    payload = {**payload, "sign": _sign_body(payload)}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


async def _make_client(app: web.Application) -> TestClient:
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.fixture
def provider():
    return HeleketProvider(MERCHANT, API_KEY, callback_url="https://host/h")


async def test_heleket_webhook_paid_marks_and_issues_invites(provider, bot_mock):
    """Happy path: valid sign + paid status → process_paid_invoice called →
    unban + invite-links called → 200 OK."""
    body = _signed_body({
        "type": "payment",
        "uuid": "heleket-uuid-1",
        "order_id": "100",
        "amount": "11.00",
        "currency": "USDT",
        "network": "tron",
        "status": "paid",
    })

    fake_paid_until = "2026-04-30T12:00:00+00:00"

    with patch(
        "app.web.heleket_webhook.process_paid_invoice",
        new=AsyncMock(return_value={
            "telegram_id": 100, "plan": "tariff_3d", "paid_until": fake_paid_until,
        }),
    ) as m_proc, patch(
        "app.web.heleket_webhook.unban_from_all_chats", new=AsyncMock(),
    ) as m_unban, patch(
        "app.web.heleket_webhook.issue_invite_links_and_send", new=AsyncMock(),
    ) as m_invite:
        app = web.Application()
        app.router.add_post("/h", make_heleket_handler(provider, bot_mock))
        client = await _make_client(app)
        try:
            resp = await client.post("/h", data=body)
            assert resp.status == 200
        finally:
            await client.close()

    m_proc.assert_awaited_once_with(
        provider="heleket",
        payment_id="heleket-uuid-1",
        webhook_amount=11.0,
    )
    m_unban.assert_awaited_once_with(bot_mock, 100)
    m_invite.assert_awaited_once_with(bot_mock, 100, fake_paid_until)


async def test_heleket_webhook_invalid_sign_returns_401(provider, bot_mock):
    body = json.dumps({
        "uuid": "x", "order_id": "1", "status": "paid", "sign": "deadbeef",
    }, separators=(",", ":")).encode()

    with patch(
        "app.web.heleket_webhook.process_paid_invoice", new=AsyncMock(),
    ) as m_proc:
        app = web.Application()
        app.router.add_post("/h", make_heleket_handler(provider, bot_mock))
        client = await _make_client(app)
        try:
            resp = await client.post("/h", data=body)
            assert resp.status == 401
        finally:
            await client.close()

    m_proc.assert_not_awaited()


async def test_heleket_webhook_paid_over_also_marks_paid(provider, bot_mock):
    body = _signed_body({
        "uuid": "uuid-over", "order_id": "200",
        "amount": "12.00", "status": "paid_over",
    })

    with patch(
        "app.web.heleket_webhook.process_paid_invoice",
        new=AsyncMock(return_value={"telegram_id": 7, "plan": "tariff_3d", "paid_until": None}),
    ) as m_proc, patch(
        "app.web.heleket_webhook.unban_from_all_chats", new=AsyncMock(),
    ), patch(
        "app.web.heleket_webhook.issue_invite_links_and_send", new=AsyncMock(),
    ):
        app = web.Application()
        app.router.add_post("/h", make_heleket_handler(provider, bot_mock))
        client = await _make_client(app)
        try:
            resp = await client.post("/h", data=body)
            assert resp.status == 200
        finally:
            await client.close()

    m_proc.assert_awaited_once()
    args, kwargs = m_proc.await_args
    assert kwargs["provider"] == "heleket"
    assert kwargs["payment_id"] == "uuid-over"


async def test_heleket_webhook_unknown_invoice_returns_200(provider, bot_mock):
    """If process_paid_invoice returns None (unknown / already paid /
    amount mismatch), webhook still returns 200 so Heleket stops retrying."""
    body = _signed_body({
        "uuid": "unknown", "order_id": "1",
        "amount": "11.00", "status": "paid",
    })

    with patch(
        "app.web.heleket_webhook.process_paid_invoice", new=AsyncMock(return_value=None),
    ), patch(
        "app.web.heleket_webhook.unban_from_all_chats", new=AsyncMock(),
    ) as m_unban, patch(
        "app.web.heleket_webhook.issue_invite_links_and_send", new=AsyncMock(),
    ) as m_invite:
        app = web.Application()
        app.router.add_post("/h", make_heleket_handler(provider, bot_mock))
        client = await _make_client(app)
        try:
            resp = await client.post("/h", data=body)
            assert resp.status == 200
        finally:
            await client.close()

    m_unban.assert_not_awaited()
    m_invite.assert_not_awaited()


async def test_heleket_webhook_idempotent(provider, bot_mock):
    """Two POSTs with the same payload: first returns telegram_id, second
    returns None (process_paid_invoice handles idempotency)."""
    body = _signed_body({
        "uuid": "idem-1", "order_id": "300",
        "amount": "11.00", "status": "paid",
    })

    proc_mock = AsyncMock(side_effect=[
        {"telegram_id": 50, "plan": "tariff_3d", "paid_until": "2026-04-30T12:00:00+00:00"},
        None,
    ])

    with patch(
        "app.web.heleket_webhook.process_paid_invoice", new=proc_mock,
    ), patch(
        "app.web.heleket_webhook.unban_from_all_chats", new=AsyncMock(),
    ) as m_unban, patch(
        "app.web.heleket_webhook.issue_invite_links_and_send", new=AsyncMock(),
    ) as m_invite:
        app = web.Application()
        app.router.add_post("/h", make_heleket_handler(provider, bot_mock))
        client = await _make_client(app)
        try:
            r1 = await client.post("/h", data=body)
            r2 = await client.post("/h", data=body)
            assert r1.status == 200
            assert r2.status == 200
        finally:
            await client.close()

    assert proc_mock.await_count == 2
    # post-payment actions only run once (when payment != None)
    m_unban.assert_awaited_once_with(bot_mock, 50)
    m_invite.assert_awaited_once_with(bot_mock, 50, "2026-04-30T12:00:00+00:00")


async def test_heleket_webhook_fail_status_does_not_mark_paid(provider, bot_mock):
    body = _signed_body({
        "uuid": "fail-1", "order_id": "1",
        "amount": "11.00", "status": "fail",
    })

    with patch(
        "app.web.heleket_webhook.process_paid_invoice", new=AsyncMock(),
    ) as m_proc:
        app = web.Application()
        app.router.add_post("/h", make_heleket_handler(provider, bot_mock))
        client = await _make_client(app)
        try:
            resp = await client.post("/h", data=body)
            assert resp.status == 200
        finally:
            await client.close()

    m_proc.assert_not_awaited()
