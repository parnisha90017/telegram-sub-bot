"""on_pay: разная стратегия для Heleket и CryptoBot.

Heleket: order_id у нас детерминированный → uuid тоже детерминированный.
Кэш pay_url в нашей БД опасен: на стороне Heleket инвойс может умереть до
истечения нашего TTL, юзер увидит «срок действия подтверждён». Поэтому
КАЖДЫЙ клик = create_invoice(is_refresh=True). По доке: для активного инвойса
is_refresh игнорируется (тот же uuid+url дёшево), для истёкшего — обновляет
address+expired_at, URL гарантированно живой. ON CONFLICT в upsert закрывает
race и кейс «тот же uuid».

CryptoBot: invoice_id уникален per-call, поэтому find_active кэш безопасен
и сохранён.

Этот файл также покрывает SQL-функции upsert_pending_payment,
find_active_pending_payment, find_expired_pending_payment, mark_pending_refreshed.
Последние две из on_pay не вызываются (always-refresh стратегия для Heleket
заменила их), но как DB-helpers оставлены и протестированы.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.payment import on_pay
from app.payments.base import Invoice


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


def _make_provider(invoice_id: str, pay_url: str, name: str = "heleket") -> MagicMock:
    p = MagicMock()
    p.create_invoice = AsyncMock(return_value=Invoice(
        provider=name,
        invoice_id=invoice_id,
        pay_url=pay_url,
        amount_usd=21.0,
    ))
    return p


# =============================================================================
# Heleket: always-refresh, no cache
# =============================================================================

async def test_on_pay_heleket_always_calls_create_invoice_with_is_refresh():
    """Каждый клик на Heleket = create_invoice(is_refresh=True). Никакого
    кэша на нашей стороне — даже если в БД есть свежая pending-запись."""
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-uuid-1", "https://pay.heleket/fresh")
    providers = {"heleket": heleket}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/fresh"),
    ) as m_upsert:
        await on_pay(cq, providers)

    heleket.create_invoice.assert_awaited_once()
    _, kwargs = heleket.create_invoice.await_args
    assert kwargs.get("is_refresh") is True, (
        f"Heleket должен дёргаться с is_refresh=True, got kwargs={kwargs}"
    )

    m_upsert.assert_awaited_once()
    _, uk = m_upsert.await_args
    assert uk["provider"] == "heleket"
    assert uk["payment_id"] == "hel-uuid-1"
    assert uk["pay_url"] == "https://pay.heleket/fresh"

    cq.message.edit_text.assert_awaited_once()
    text = cq.message.edit_text.await_args.args[0]
    assert "https://pay.heleket/fresh" in text


async def test_on_pay_heleket_does_not_call_find_active_pending_payment():
    """Регрессия: для Heleket find_active_pending_payment НЕ вызывается.
    Если он вернёт мёртвый pay_url из БД, юзер увидит «срок подтверждён»."""
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-uuid-2", "https://pay.heleket/url2")
    providers = {"heleket": heleket}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(),
    ) as m_find_active, patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/url2"),
    ):
        await on_pay(cq, providers)

    m_find_active.assert_not_awaited()
    heleket.create_invoice.assert_awaited_once()


async def test_on_pay_heleket_handles_provider_returns_existing_payment_id():
    """Heleket для того же order_id возвращает тот же uuid: ON CONFLICT в
    upsert_pending_payment не падает, RETURNING отдаёт фактический pay_url
    (может отличаться если на стороне Heleket случился refresh address)."""
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-stable-uuid", "https://pay.heleket/v2")
    providers = {"heleket": heleket}

    # upsert через ON CONFLICT возвращает то, что только что записали.
    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/v2"),
    ) as m_upsert:
        await on_pay(cq, providers)

    heleket.create_invoice.assert_awaited_once()
    _, ck = heleket.create_invoice.await_args
    assert ck.get("is_refresh") is True
    m_upsert.assert_awaited_once()
    cq.message.edit_text.assert_awaited_once()
    text = cq.message.edit_text.await_args.args[0]
    assert "https://pay.heleket/v2" in text


async def test_on_pay_heleket_invoice_creation_failure_shows_error():
    """create_invoice бросает (network/Heleket-down) → юзеру INVOICE_ERROR,
    в БД ничего не пишется."""
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = MagicMock()
    heleket.create_invoice = AsyncMock(side_effect=RuntimeError("heleket down"))
    providers = {"heleket": heleket}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment", new=AsyncMock(),
    ) as m_upsert:
        await on_pay(cq, providers)

    m_upsert.assert_not_awaited()
    cq.message.answer.assert_awaited_once()


# =============================================================================
# CryptoBot: cache via find_active stays
# =============================================================================

async def test_on_pay_cryptobot_still_uses_find_active_cache():
    """Регрессия: для CryptoBot find_active вызывается КАК РАНЬШЕ. Если
    есть свежий pending — отдаём из БД, в провайдер не идём."""
    cq = _mock_callback_query("pay:tariff_7d:cryptobot", tg_id=42)
    cryptobot = _make_provider("cb-uuid", "https://nope", name="cryptobot")
    providers = {"cryptobot": cryptobot}

    existing = {
        "id": 11,
        "payment_id": "cb-cached",
        "pay_url": "https://t.me/CryptoBot?start=cb-cached",
        "created_at": datetime.now(timezone.utc),
    }

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=existing),
    ) as m_find, patch(
        "app.bot.handlers.payment.upsert_pending_payment", new=AsyncMock(),
    ) as m_upsert:
        await on_pay(cq, providers)

    m_find.assert_awaited_once()
    cryptobot.create_invoice.assert_not_awaited()
    m_upsert.assert_not_awaited()

    cq.message.edit_text.assert_awaited_once()
    text = cq.message.edit_text.await_args.args[0]
    assert "https://t.me/CryptoBot?start=cb-cached" in text


async def test_on_pay_cryptobot_creates_new_invoice_when_no_cache():
    """Happy path для CryptoBot: pending в БД нет → create_invoice БЕЗ
    is_refresh (CryptoBot про этот параметр не знает) + upsert."""
    cq = _mock_callback_query("pay:tariff_7d:cryptobot", tg_id=42)
    cryptobot = _make_provider(
        "cb-new", "https://t.me/CryptoBot?start=cb-new", name="cryptobot",
    )
    providers = {"cryptobot": cryptobot}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://t.me/CryptoBot?start=cb-new"),
    ) as m_upsert:
        await on_pay(cq, providers)

    cryptobot.create_invoice.assert_awaited_once()
    _, ck = cryptobot.create_invoice.await_args
    # is_refresh для CryptoBot НЕ передаётся.
    assert "is_refresh" not in ck
    m_upsert.assert_awaited_once()
    _, uk = m_upsert.await_args
    assert uk["provider"] == "cryptobot"
    assert uk["payment_id"] == "cb-new"


# =============================================================================
# DB helpers: upsert_pending_payment, find_active, find_expired, mark_refreshed
# (последние две больше не вызываются из on_pay, но оставлены как DB-helpers
# и протестированы здесь же)
# =============================================================================

async def test_upsert_pending_payment_handles_conflict():
    """SQL содержит ON CONFLICT (provider, payment_id) DO UPDATE RETURNING.
    Два вызова с одной парой — оба возвращают URL, не падают."""
    from app.db.queries import upsert_pending_payment

    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=[
        {"pay_url": "https://pay.heleket/u1"},
        {"pay_url": "https://pay.heleket/u2"},
    ])

    with patch("app.db.queries.get_pool", return_value=pool):
        url1 = await upsert_pending_payment(
            telegram_id=42, plan="tariff_7d", amount=21,
            payment_id="hel-same", provider="heleket",
            pay_url="https://pay.heleket/u1",
        )
        url2 = await upsert_pending_payment(
            telegram_id=42, plan="tariff_7d", amount=21,
            payment_id="hel-same", provider="heleket",
            pay_url="https://pay.heleket/u2",
        )

    assert url1 == "https://pay.heleket/u1"
    assert url2 == "https://pay.heleket/u2"
    assert pool.fetchrow.await_count == 2

    sql = pool.fetchrow.await_args_list[0].args[0]
    assert "ON CONFLICT" in sql
    assert "(provider, payment_id)" in sql
    assert "DO UPDATE" in sql
    assert "RETURNING pay_url" in sql


async def test_find_active_pending_payment_sql_filters_correctly():
    """SQL: pay_url IS NOT NULL (legacy без URL пропускаем), max_age параметром."""
    from app.db.queries import find_active_pending_payment

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    with patch("app.db.queries.get_pool", return_value=pool):
        await find_active_pending_payment(
            telegram_id=42, plan="tariff_7d", provider="cryptobot",
            max_age_seconds=1800,
        )

    sql = pool.fetchrow.await_args.args[0]
    args = pool.fetchrow.await_args.args
    assert "pay_url IS NOT NULL" in sql
    assert "status = 'pending'" in sql
    assert 1800 in args
    assert "make_interval" in sql


async def test_find_expired_pending_payment_returns_only_old_records():
    """DB-helper оставлен (вне on_pay). SQL: created_at <= NOW() - max_age,
    pay_url IS NOT NULL, max_age параметром."""
    from app.db.queries import find_expired_pending_payment

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    with patch("app.db.queries.get_pool", return_value=pool):
        await find_expired_pending_payment(
            telegram_id=42, plan="tariff_7d", provider="heleket",
            max_age_seconds=3600,
        )

    sql = pool.fetchrow.await_args.args[0]
    args = pool.fetchrow.await_args.args
    assert "created_at <= NOW() - make_interval(secs => $4)" in sql
    assert "created_at >" not in sql
    assert "pay_url IS NOT NULL" in sql
    assert "status = 'pending'" in sql
    assert 3600 in args


async def test_mark_pending_refreshed_updates_created_at_and_pay_url():
    """DB-helper оставлен (вне on_pay). SQL: SET pay_url + created_at = NOW(),
    WHERE provider + payment_id."""
    from app.db.queries import mark_pending_refreshed

    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")

    with patch("app.db.queries.get_pool", return_value=pool):
        await mark_pending_refreshed(
            provider="heleket",
            payment_id="hel-uuid",
            pay_url="https://pay.heleket/refreshed",
        )

    pool.execute.assert_awaited_once()
    sql = pool.execute.await_args.args[0]
    args = pool.execute.await_args.args[1:]

    assert "created_at = NOW()" in sql
    assert "pay_url = $3" in sql or "pay_url=$3" in sql.replace(" ", "")
    assert "provider = $1" in sql
    assert "payment_id = $2" in sql
    assert args == ("heleket", "hel-uuid", "https://pay.heleket/refreshed")


# =============================================================================
# Heleket provider: HTTP-уровень — is_refresh реально попадает в body
# =============================================================================

async def test_create_invoice_passes_is_refresh_to_heleket_api():
    """Провайдер кладёт "is_refresh": true в body запроса; без флага — поля нет."""
    from aioresponses import aioresponses

    from app.payments.heleket import HELEKET_API_URL, HeleketProvider

    provider = HeleketProvider(
        merchant_uuid="m-uuid",
        api_key="secret",
        callback_url="https://bot/cb",
    )

    with aioresponses() as mocked:
        mocked.post(HELEKET_API_URL, payload={
            "state": 0,
            "result": {"uuid": "hel-uuid", "url": "https://pay.heleket/x"},
        })
        await provider.create_invoice(
            amount_usd=21.0,
            order_id="42_tariff_7d",
            is_refresh=True,
        )
        req = list(mocked.requests.values())[-1][-1]
        sent_body = req.kwargs["data"]
        assert b'"is_refresh":true' in sent_body, (
            f"is_refresh:true должен быть в body, got: {sent_body!r}"
        )

    with aioresponses() as mocked2:
        mocked2.post(HELEKET_API_URL, payload={
            "state": 0,
            "result": {"uuid": "hel-uuid-2", "url": "https://pay.heleket/y"},
        })
        await provider.create_invoice(
            amount_usd=21.0,
            order_id="42_tariff_7d",
        )
        req = list(mocked2.requests.values())[-1][-1]
        sent_body = req.kwargs["data"]
        assert b"is_refresh" not in sent_body, (
            f"is_refresh должен отсутствовать когда не передан, got: {sent_body!r}"
        )
