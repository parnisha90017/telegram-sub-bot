"""/my — пользовательская команда показа состояния подписки.

Текст не должен упоминать plan/tariff_* — это вводит в заблуждение при
продлении (как уже исправили в reminder)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.start import _render_my_subscription, cmd_my


def _make_message(tg_id: int = 100) -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = tg_id
    msg.answer = AsyncMock()
    return msg


async def test_my_shows_remaining_for_active_user():
    paid_until = datetime.now(timezone.utc) + timedelta(days=5, hours=23)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "alice", "plan": "tariff_7d",
            "paid_until": paid_until, "status": "active",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "5 дн." in text
    # Дата paid_until попала в ответ (день/месяц/год точно совпадают)
    assert paid_until.strftime("%d.%m.%Y") in text
    assert "✅ активна" in text


async def test_my_shows_none_for_user_without_subscription():
    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": None,
            "paid_until": None, "status": "new",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    text = msg.answer.await_args.args[0]
    assert "нет активной подписки" in text


async def test_my_shows_none_for_expired_user():
    paid_until = datetime.now(timezone.utc) - timedelta(hours=1)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": "tariff_3d",
            "paid_until": paid_until, "status": "expired",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    text = msg.answer.await_args.args[0]
    assert "нет активной подписки" in text


async def test_my_shows_hours_for_subscription_under_24h():
    paid_until = datetime.now(timezone.utc) + timedelta(hours=18)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": "tariff_3d",
            "paid_until": paid_until, "status": "active",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    text = msg.answer.await_args.args[0]
    # 17 или 18 ч в зависимости от микросекундного дрейфа
    assert "17 ч." in text or "18 ч." in text
    # Без "дн." — подписка короче суток
    assert "дн." not in text


async def test_my_text_does_not_mention_plan():
    """Регрессия: даже если у юзера plan='tariff_3d' в БД — в тексте /my
    его быть не должно."""
    paid_until = datetime.now(timezone.utc) + timedelta(days=10)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": "tariff_3d",
            "paid_until": paid_until, "status": "active",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    text = msg.answer.await_args.args[0]
    assert "tariff" not in text.lower()
    assert "3д" not in text
    assert "tariff_3d" not in text


async def test_my_for_unknown_user_shows_none():
    """Юзер вообще не делал /start — в users его нет → MY_SUBSCRIPTION_NONE."""
    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value=None),
    ):
        msg = _make_message(999)
        await cmd_my(msg)

    text = msg.answer.await_args.args[0]
    assert "нет активной подписки" in text


async def test_my_shows_minutes_for_under_1h():
    """paid_until через 30 минут — должны увидеть минуты, не нули в часах."""
    paid_until = datetime.now(timezone.utc) + timedelta(minutes=30)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": None,
            "paid_until": paid_until, "status": "active",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    text = msg.answer.await_args.args[0]
    assert "мин." in text
    assert "дн." not in text
    assert "ч." not in text  # под часом — только минуты


async def test_render_my_subscription_unit():
    """Прямой вызов helper'а — проверяем формат строк."""
    paid_until = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 1, "username": "x", "plan": None,
            "paid_until": paid_until, "status": "active",
        }),
    ):
        text = await _render_my_subscription(1)

    assert "15.05.2026 12:00 UTC" in text
    assert "активна" in text


# -----------------------------------------------------------------------------
# Кнопка «Получить ссылки заново» в /my
# -----------------------------------------------------------------------------

def _kb_callback_data_set(reply_markup) -> set[str]:
    """Все callback_data в InlineKeyboardMarkup ответа /my."""
    if reply_markup is None or not getattr(reply_markup, "inline_keyboard", None):
        return set()
    return {
        btn.callback_data
        for row in reply_markup.inline_keyboard
        for btn in row
        if btn.callback_data is not None
    }


async def test_my_active_user_sees_reissue_button():
    paid_until = datetime.now(timezone.utc) + timedelta(days=5)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "alice", "plan": "tariff_7d",
            "paid_until": paid_until, "status": "active",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    msg.answer.assert_awaited_once()
    kwargs = msg.answer.await_args.kwargs
    assert "user:reissue_links" in _kb_callback_data_set(kwargs.get("reply_markup"))


async def test_my_expired_user_no_button():
    paid_until = datetime.now(timezone.utc) - timedelta(hours=1)

    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": "tariff_3d",
            "paid_until": paid_until, "status": "expired",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    kwargs = msg.answer.await_args.kwargs
    assert "user:reissue_links" not in _kb_callback_data_set(kwargs.get("reply_markup"))


async def test_my_new_user_no_button():
    """status='new', paid_until=None — кнопки быть не должно."""
    with patch(
        "app.bot.handlers.start.get_user_by_tg_id",
        new=AsyncMock(return_value={
            "telegram_id": 100, "username": "u", "plan": None,
            "paid_until": None, "status": "new",
        }),
    ):
        msg = _make_message(100)
        await cmd_my(msg)

    kwargs = msg.answer.await_args.kwargs
    assert "user:reissue_links" not in _kb_callback_data_set(kwargs.get("reply_markup"))
