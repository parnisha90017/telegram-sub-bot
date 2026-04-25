"""Tests for issue_invite_links_and_send: must use creates_join_request=True
(NOT member_limit), and must register every link in granted_access."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.chats.manager import issue_invite_links_and_send


CHAT_IDS = [-1001, -1002, -1003, -1004]


def _make_bot_returning_links() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    counter = {"i": 0}

    async def _create(**kwargs):
        counter["i"] += 1
        result = MagicMock()
        result.invite_link = f"https://t.me/+inv{counter['i']}"
        return result

    bot.create_chat_invite_link = AsyncMock(side_effect=_create)
    return bot


async def test_invite_link_uses_creates_join_request_true():
    bot = _make_bot_returning_links()
    paid_until = datetime.now(timezone.utc) + timedelta(days=7)

    with patch("app.chats.manager.settings") as m_settings, patch(
        "app.chats.manager.grant_access", new=AsyncMock(),
    ):
        m_settings.chat_ids = CHAT_IDS
        await issue_invite_links_and_send(bot, telegram_id=42, paid_until=paid_until)

    # Должны быть 4 вызова — по числу чатов
    assert bot.create_chat_invite_link.await_count == 4
    for call in bot.create_chat_invite_link.await_args_list:
        kwargs = call.kwargs
        assert kwargs.get("creates_join_request") is True
        # member_limit ЗАПРЕЩЁН при creates_join_request=True (Telegram отвергает)
        assert "member_limit" not in kwargs
        # expire_date должен быть установлен (1 час)
        assert kwargs.get("expire_date") is not None


async def test_invite_link_recorded_in_granted_access():
    bot = _make_bot_returning_links()
    paid_until = datetime.now(timezone.utc) + timedelta(days=7)

    with patch("app.chats.manager.settings") as m_settings, patch(
        "app.chats.manager.grant_access", new=AsyncMock(),
    ) as m_grant:
        m_settings.chat_ids = CHAT_IDS
        await issue_invite_links_and_send(bot, telegram_id=42, paid_until=paid_until)

    # 4 чата → 4 grant_access
    assert m_grant.await_count == 4
    for i, call in enumerate(m_grant.await_args_list):
        args = call.args
        # signature: grant_access(telegram_id, chat_id, invite_link, paid_until)
        assert args[0] == 42                           # telegram_id
        assert args[1] in CHAT_IDS                     # chat_id
        assert args[2].startswith("https://t.me/+inv")  # invite_link
        assert args[3] == paid_until                   # paid_until


async def test_send_message_called_after_links_collected():
    bot = _make_bot_returning_links()
    paid_until = datetime.now(timezone.utc) + timedelta(days=7)

    with patch("app.chats.manager.settings") as m_settings, patch(
        "app.chats.manager.grant_access", new=AsyncMock(),
    ):
        m_settings.chat_ids = CHAT_IDS
        await issue_invite_links_and_send(bot, telegram_id=42, paid_until=paid_until)

    bot.send_message.assert_awaited_once()
    args, kwargs = bot.send_message.await_args
    # Сообщение пользователю с 4 ссылками
    assert args[0] == 42  # telegram_id
    text = args[1]
    for i in range(1, 5):
        assert f"https://t.me/+inv{i}" in text


async def test_no_send_when_all_invite_links_failed():
    """Если bot.create_chat_invite_link упал во всех 4 чатах — send_message не вызывается."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.create_chat_invite_link = AsyncMock(side_effect=Exception("API down"))
    paid_until = datetime.now(timezone.utc) + timedelta(days=7)

    with patch("app.chats.manager.settings") as m_settings, patch(
        "app.chats.manager.grant_access", new=AsyncMock(),
    ) as m_grant:
        m_settings.chat_ids = CHAT_IDS
        await issue_invite_links_and_send(bot, telegram_id=42, paid_until=paid_until)

    bot.send_message.assert_not_awaited()
    m_grant.assert_not_awaited()


async def test_grant_access_failure_skips_link():
    """Если grant_access падает на одном чате — соответствующая ссылка не уходит юзеру.
    (Защита: без записи в БД ChatJoinRequest будет declined.)"""
    bot = _make_bot_returning_links()
    paid_until = datetime.now(timezone.utc) + timedelta(days=7)

    # grant_access падает только на втором вызове
    grant_calls = {"i": 0}

    async def _grant(*args, **kwargs):
        grant_calls["i"] += 1
        if grant_calls["i"] == 2:
            raise RuntimeError("DB hiccup")

    with patch("app.chats.manager.settings") as m_settings, patch(
        "app.chats.manager.grant_access", new=AsyncMock(side_effect=_grant),
    ):
        m_settings.chat_ids = CHAT_IDS
        await issue_invite_links_and_send(bot, telegram_id=42, paid_until=paid_until)

    bot.send_message.assert_awaited_once()
    args, _ = bot.send_message.await_args
    text = args[1]
    # Юзеру уходят 3 ссылки (одна — пропущена из-за упавшего grant_access)
    link_lines = [ln for ln in text.split("\n") if "https://t.me/+inv" in ln]
    assert len(link_lines) == 3
