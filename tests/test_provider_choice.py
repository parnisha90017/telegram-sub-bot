"""Tests for the provider-selection UI added on top of the existing /start flow.

We test:
  * provider_pick_kb keyboard rendering for various enabled_providers configs;
  * on_buy callback now redirects to provider picker (does NOT create invoice);
  * on_pay callback creates invoice via the chosen provider only;
  * on_pay rejects providers absent from the dispatched providers dict.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.payment import on_buy, on_pay
from app.bot.keyboards import PROVIDER_LABELS, provider_pick_kb
from app.payments.base import Invoice


# -----------------------------------------------------------------------------
# Keyboard
# -----------------------------------------------------------------------------

def _button_texts(kb) -> list[str]:
    return [btn.text for row in kb.inline_keyboard for btn in row]


def _button_callbacks(kb) -> list[str]:
    return [btn.callback_data for row in kb.inline_keyboard for btn in row]


def test_provider_pick_kb_shows_only_enabled_cryptobot():
    kb = provider_pick_kb("tariff_3d", ["cryptobot"])
    texts = _button_texts(kb)
    cbs = _button_callbacks(kb)
    assert PROVIDER_LABELS["cryptobot"] in texts
    assert PROVIDER_LABELS["heleket"] not in texts
    assert "pay:tariff_3d:cryptobot" in cbs
    assert not any(c and c.endswith(":heleket") for c in cbs)


def test_provider_pick_kb_shows_both_when_enabled():
    kb = provider_pick_kb("tariff_7d", ["cryptobot", "heleket"])
    texts = _button_texts(kb)
    cbs = _button_callbacks(kb)
    assert PROVIDER_LABELS["cryptobot"] in texts
    assert PROVIDER_LABELS["heleket"] in texts
    assert "pay:tariff_7d:cryptobot" in cbs
    assert "pay:tariff_7d:heleket" in cbs


def test_provider_pick_kb_always_has_back_button():
    kb = provider_pick_kb("tariff_3d", ["heleket"])
    cbs = _button_callbacks(kb)
    assert "show_plans" in cbs


# -----------------------------------------------------------------------------
# on_buy → shows provider picker (no invoice yet)
# -----------------------------------------------------------------------------

def _mock_callback_query(data: str, tg_id: int = 999) -> MagicMock:
    cq = MagicMock(spec=CallbackQuery)
    cq.data = data
    user = MagicMock()
    user.id = tg_id
    user.username = "u"
    cq.from_user = user
    cq.message = MagicMock(spec=Message)
    cq.message.edit_text = AsyncMock()
    cq.message.answer = AsyncMock()
    cq.answer = AsyncMock()
    return cq


async def test_on_buy_shows_provider_picker_does_not_create_invoice():
    cq = _mock_callback_query("buy:tariff_3d")
    settings = MagicMock()
    settings.enabled_providers = ["cryptobot", "heleket"]

    await on_buy(cq, settings)

    cq.message.edit_text.assert_awaited_once()
    args, kwargs = cq.message.edit_text.await_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Выбери способ оплаты" in text or "способ оплаты" in text.lower()
    kb = kwargs.get("reply_markup")
    assert kb is not None
    cbs = _button_callbacks(kb)
    assert "pay:tariff_3d:cryptobot" in cbs
    assert "pay:tariff_3d:heleket" in cbs


async def test_on_buy_unknown_plan_alerts():
    cq = _mock_callback_query("buy:tariff_999")
    settings = MagicMock()
    settings.enabled_providers = ["cryptobot"]

    await on_buy(cq, settings)

    cq.answer.assert_awaited()
    cq.message.edit_text.assert_not_awaited()


# -----------------------------------------------------------------------------
# on_pay → creates invoice via the chosen provider
# -----------------------------------------------------------------------------

async def test_on_pay_creates_invoice_via_chosen_provider():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)

    heleket_provider = MagicMock()
    heleket_provider.create_invoice = AsyncMock(return_value=Invoice(
        provider="heleket",
        invoice_id="hel-inv-1",
        pay_url="https://pay.heleket/abc",
        amount_usd=21.0,
    ))
    cryptobot_provider = MagicMock()
    cryptobot_provider.create_invoice = AsyncMock()

    providers = {"cryptobot": cryptobot_provider, "heleket": heleket_provider}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.find_expired_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/abc"),
    ) as m_upsert:
        await on_pay(cq, providers)

    # Heleket was called, CryptoBot was not.
    heleket_provider.create_invoice.assert_awaited_once()
    cryptobot_provider.create_invoice.assert_not_awaited()
    # And persisted with provider="heleket".
    m_upsert.assert_awaited_once()
    _, kwargs = m_upsert.await_args
    assert kwargs["provider"] == "heleket"
    assert kwargs["payment_id"] == "hel-inv-1"
    assert kwargs["pay_url"] == "https://pay.heleket/abc"
    # User got the pay URL.
    cq.message.edit_text.assert_awaited_once()
    args, kwargs = cq.message.edit_text.await_args
    text = args[0] if args else kwargs.get("text", "")
    assert "https://pay.heleket/abc" in text


async def test_on_pay_rejects_disabled_provider():
    cq = _mock_callback_query("pay:tariff_3d:heleket", tg_id=42)
    providers = {"cryptobot": MagicMock(create_invoice=AsyncMock())}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment", new=AsyncMock(),
    ) as m_upsert:
        await on_pay(cq, providers)

    cq.answer.assert_awaited()
    m_upsert.assert_not_awaited()


async def test_on_pay_unknown_plan_alerts():
    cq = _mock_callback_query("pay:tariff_xxx:cryptobot")
    providers = {"cryptobot": MagicMock(create_invoice=AsyncMock())}

    await on_pay(cq, providers)

    providers["cryptobot"].create_invoice.assert_not_awaited()
