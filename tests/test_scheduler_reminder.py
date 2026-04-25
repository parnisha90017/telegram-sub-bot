"""remind_24h: текст должен показывать РЕАЛЬНЫЙ paid_until и остаток в
часах, БЕЗ упоминания тарифа (plan). Тариф вводит в заблуждение при
продлении (7+3 дня → plan хранится последний 3д, но остаток = 10 дней)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramForbiddenError

from app.scheduler.jobs import remind_24h


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


async def test_reminder_text_includes_paid_until_and_hours_not_plan():
    """Контракт текста: дата paid_until + часы; ни 'tariff_3d', ни '7d'."""
    bot = _make_bot()
    paid_until = datetime(2026, 5, 1, 19, 52, tzinfo=timezone.utc)

    with patch(
        "app.scheduler.jobs.list_expiring_between",
        new=AsyncMock(return_value=[
            {"telegram_id": 100, "paid_until": paid_until},
        ]),
    ):
        await remind_24h(bot)

    bot.send_message.assert_awaited_once()
    args, _ = bot.send_message.await_args
    assert args[0] == 100
    text = args[1]

    # Дата paid_until попала в сообщение.
    assert "01.05.2026" in text
    assert "19:52" in text
    assert "UTC" in text

    # Упоминание часов есть.
    assert " ч" in text or "час" in text.lower()

    # Никаких следов plan.
    assert "tariff_3d" not in text
    assert "tariff_7d" not in text
    assert "tariff_30d" not in text
    assert "tariff" not in text.lower()


async def test_reminder_only_for_users_in_window():
    """remind_24h берёт юзеров из list_expiring_between(now+23:30, now+24:30) —
    мокаем функцию, проверяем что send_message зовётся ровно для тех, кого
    она вернула."""
    bot = _make_bot()
    paid_until_24h = datetime.now(timezone.utc) + timedelta(hours=24)

    # Окно фильтрует list_expiring_between (это тестируется в БД-тесте); здесь
    # мокаем функцию, симулируем что только один юзер попал в окно.
    with patch(
        "app.scheduler.jobs.list_expiring_between",
        new=AsyncMock(return_value=[{"telegram_id": 1, "paid_until": paid_until_24h}]),
    ) as m_list:
        await remind_24h(bot)

    bot.send_message.assert_awaited_once()
    sent_to = bot.send_message.await_args.args[0]
    assert sent_to == 1

    # Запрос к БД был с правильным окном (~24h ± 30мин)
    m_list.assert_awaited_once()
    args, _ = m_list.await_args
    window_start, window_end = args
    delta = (window_end - window_start).total_seconds()
    assert 50 * 60 < delta < 70 * 60, f"window width {delta}s not ~1h"


async def test_reminder_handles_send_failure_gracefully():
    """Если bot.send_message бросает (юзер заблокировал бота) — продолжаем
    рассылку другим без падения всей джобы."""
    bot = _make_bot()
    paid_until = datetime.now(timezone.utc) + timedelta(hours=24)

    bot.send_message = AsyncMock(side_effect=[
        TelegramForbiddenError(method=MagicMock(), message="bot blocked"),  # юзер 1
        None,                                                                # юзер 2
        Exception("network glitch"),                                         # юзер 3
        None,                                                                # юзер 4
    ])

    with patch(
        "app.scheduler.jobs.list_expiring_between",
        new=AsyncMock(return_value=[
            {"telegram_id": 1, "paid_until": paid_until},
            {"telegram_id": 2, "paid_until": paid_until},
            {"telegram_id": 3, "paid_until": paid_until},
            {"telegram_id": 4, "paid_until": paid_until},
        ]),
    ):
        # Не должно бросать наружу.
        await remind_24h(bot)

    assert bot.send_message.await_count == 4


async def test_reminder_hours_left_reflects_real_delta():
    """hours_left = floor((paid_until - now) / 3600). При paid_until через
    24h 15мин ожидаем '24' в тексте; через 23h 45мин — '23'."""
    bot = _make_bot()
    now = datetime.now(timezone.utc)
    paid_until_24_15 = now + timedelta(hours=24, minutes=15)

    with patch(
        "app.scheduler.jobs.list_expiring_between",
        new=AsyncMock(return_value=[{"telegram_id": 99, "paid_until": paid_until_24_15}]),
    ):
        await remind_24h(bot)

    text = bot.send_message.await_args.args[1]
    assert "24 ч" in text or "24 час" in text.lower()


async def test_reminder_does_not_fire_for_empty_window():
    bot = _make_bot()

    with patch(
        "app.scheduler.jobs.list_expiring_between",
        new=AsyncMock(return_value=[]),
    ):
        await remind_24h(bot)

    bot.send_message.assert_not_awaited()
