"""Reuse-pending-invoice flow в on_pay (повторный клик на тариф).

Регрессия прода: при повторном клике с тем же (tg, plan) Heleket возвращает
тот же uuid → INSERT падал с UniqueViolationError, юзер залипал на «Загрузка».

Теперь:
  * перед обращением к провайдеру ищем свежий pending (≤1ч) с pay_url;
    если есть — отдаём тот же URL, провайдер не дёргается;
  * upsert с ON CONFLICT (provider, payment_id) DO UPDATE — закрывает кейс
    «провайдер вернул тот же uuid» и race;
  * find_active_pending_payment игнорирует записи с pay_url IS NULL
    (legacy до миграции) — для них пройдёт полный цикл и upsert проставит URL.
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


def _make_provider(invoice_id: str, pay_url: str) -> MagicMock:
    p = MagicMock()
    p.create_invoice = AsyncMock(return_value=Invoice(
        provider="heleket",
        invoice_id=invoice_id,
        pay_url=pay_url,
        amount_usd=21.0,
    ))
    return p


# -----------------------------------------------------------------------------
# 1) happy path: pending в БД нет — создаём новый
# -----------------------------------------------------------------------------

async def test_on_pay_creates_new_invoice():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-new-1", "https://pay.heleket/new")
    providers = {"heleket": heleket}

    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ) as m_find, patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/new"),
    ) as m_upsert:
        await on_pay(cq, providers)

    m_find.assert_awaited_once()
    heleket.create_invoice.assert_awaited_once()
    m_upsert.assert_awaited_once()
    _, kwargs = m_upsert.await_args
    assert kwargs["payment_id"] == "hel-new-1"
    assert kwargs["pay_url"] == "https://pay.heleket/new"

    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    assert "https://pay.heleket/new" in args[0]


# -----------------------------------------------------------------------------
# 2) reuse: уже есть свежий pending с pay_url — провайдер не дёргается
# -----------------------------------------------------------------------------

async def test_on_pay_reuses_pending_invoice():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    heleket = _make_provider("hel-should-not-be-called", "https://nope")
    providers = {"heleket": heleket}

    existing = {
        "id": 11,
        "payment_id": "hel-existing-1",
        "pay_url": "https://pay.heleket/existing",
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
    # Провайдер НЕ вызывался — это главное.
    heleket.create_invoice.assert_not_awaited()
    # Upsert тоже не зовём — запись уже есть, ничего обновлять не надо.
    m_upsert.assert_not_awaited()

    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    assert "https://pay.heleket/existing" in args[0]


# -----------------------------------------------------------------------------
# 3) Heleket вернул payment_id, который уже в БД (race / legacy)
#    find ничего не нашёл (pay_url IS NULL у старой записи), идём в провайдер,
#    upsert через ON CONFLICT возвращает фактический pay_url. Не падаем.
# -----------------------------------------------------------------------------

async def test_on_pay_handles_provider_returns_existing_payment_id():
    cq = _mock_callback_query("pay:tariff_7d:heleket", tg_id=42)
    # Heleket вернул тот же uuid что был в БД (legacy pending без pay_url)
    heleket = _make_provider("hel-collision-uuid", "https://pay.heleket/fresh")
    providers = {"heleket": heleket}

    # find игнорирует legacy-запись (pay_url IS NULL) → возвращает None
    # upsert через ON CONFLICT DO UPDATE прописывает pay_url в существующую
    # запись; RETURNING отдаёт ровно то, что только что записали.
    with patch(
        "app.bot.handlers.payment.upsert_user", new=AsyncMock(),
    ), patch(
        "app.bot.handlers.payment.find_active_pending_payment",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.bot.handlers.payment.upsert_pending_payment",
        new=AsyncMock(return_value="https://pay.heleket/fresh"),
    ) as m_upsert:
        await on_pay(cq, providers)

    heleket.create_invoice.assert_awaited_once()
    m_upsert.assert_awaited_once()
    _, kwargs = m_upsert.await_args
    assert kwargs["payment_id"] == "hel-collision-uuid"
    assert kwargs["pay_url"] == "https://pay.heleket/fresh"

    cq.message.edit_text.assert_awaited_once()
    args, _ = cq.message.edit_text.await_args
    # Юзер видит URL — не «Загрузка».
    assert "https://pay.heleket/fresh" in args[0]


# -----------------------------------------------------------------------------
# 4) upsert_pending_payment идемпотентен: два вызова с одной (provider, payment_id)
#    второй возвращает pay_url первого (или новый, если переписали) и не падает.
#    SQL-уровень: ON CONFLICT DO UPDATE SET pay_url = EXCLUDED.pay_url RETURNING.
# -----------------------------------------------------------------------------

async def test_upsert_pending_payment_handles_conflict():
    """Мокаем asyncpg-pool. Первый вызов вставляет, второй — попадает в
    DO UPDATE и возвращает обновлённый pay_url. Никаких исключений."""
    from app.db.queries import upsert_pending_payment

    pool = MagicMock()
    # При INSERT возвращаем то, что прилетело в EXCLUDED (имитация RETURNING).
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

    # SQL содержит ON CONFLICT DO UPDATE и RETURNING — это и есть гарантия.
    sql = pool.fetchrow.await_args_list[0].args[0]
    assert "ON CONFLICT" in sql
    assert "(provider, payment_id)" in sql
    assert "DO UPDATE" in sql
    assert "RETURNING pay_url" in sql


# -----------------------------------------------------------------------------
# 5) find_active_pending_payment SQL-проверка: фильтр pay_url IS NOT NULL
#    + max_age параметр (не зашитый литерал).
# -----------------------------------------------------------------------------

async def test_find_active_pending_payment_sql_filters_correctly():
    """Регрессия: SELECT должен иметь pay_url IS NOT NULL (legacy-записи без
    URL пропускаем) и принимать max_age_seconds параметром."""
    from app.db.queries import find_active_pending_payment

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)

    with patch("app.db.queries.get_pool", return_value=pool):
        await find_active_pending_payment(
            telegram_id=42, plan="tariff_7d", provider="heleket",
            max_age_seconds=1800,
        )

    sql = pool.fetchrow.await_args.args[0]
    args = pool.fetchrow.await_args.args
    assert "pay_url IS NOT NULL" in sql
    assert "status = 'pending'" in sql
    # max_age пробрасывается параметром, не литералом.
    assert 1800 in args
    assert "make_interval" in sql
