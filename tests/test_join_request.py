"""Tests for the chat_join_request handler.

Pipeline being tested: ChatJoinRequest → find_granted_by_link → check
user_id matches buyer + subscription not expired → approve/decline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.join_request import on_join_request


def _make_request(
    user_id: int,
    chat_id: int,
    invite_link_url: str | None,
) -> MagicMock:
    request = MagicMock()
    request.from_user = MagicMock()
    request.from_user.id = user_id
    request.chat = MagicMock()
    request.chat.id = chat_id
    if invite_link_url is None:
        request.invite_link = None
    else:
        request.invite_link = MagicMock()
        request.invite_link.invite_link = invite_link_url
    request.approve = AsyncMock()
    request.decline = AsyncMock()
    return request


async def test_join_approved_when_user_matches_buyer():
    request = _make_request(user_id=123, chat_id=-1001, invite_link_url="https://t.me/+abc")
    paid_until = datetime.now(timezone.utc) + timedelta(days=3)

    with patch(
        "app.bot.handlers.join_request.find_granted_by_link",
        new=AsyncMock(return_value={
            "id": 1, "telegram_id": 123, "chat_id": -1001,
            "invite_link": "https://t.me/+abc", "paid_until": paid_until,
            "joined_at": None, "revoked_at": None,
        }),
    ), patch(
        "app.bot.handlers.join_request.mark_joined", new=AsyncMock(),
    ) as m_mark:
        await on_join_request(request)

    request.approve.assert_awaited_once()
    request.decline.assert_not_awaited()
    m_mark.assert_awaited_once_with("https://t.me/+abc")


async def test_join_declined_when_different_user():
    """Покупатель А получил ссылку, но войти пытается Б — должен быть decline."""
    request = _make_request(user_id=999, chat_id=-1001, invite_link_url="https://t.me/+abc")
    paid_until = datetime.now(timezone.utc) + timedelta(days=3)

    with patch(
        "app.bot.handlers.join_request.find_granted_by_link",
        new=AsyncMock(return_value={
            "id": 1, "telegram_id": 123, "chat_id": -1001,
            "invite_link": "https://t.me/+abc", "paid_until": paid_until,
            "joined_at": None, "revoked_at": None,
        }),
    ), patch(
        "app.bot.handlers.join_request.mark_joined", new=AsyncMock(),
    ) as m_mark:
        await on_join_request(request)

    request.decline.assert_awaited_once()
    request.approve.assert_not_awaited()
    m_mark.assert_not_awaited()


async def test_join_declined_when_subscription_expired():
    request = _make_request(user_id=123, chat_id=-1001, invite_link_url="https://t.me/+abc")
    paid_until = datetime.now(timezone.utc) - timedelta(hours=1)  # истёк

    with patch(
        "app.bot.handlers.join_request.find_granted_by_link",
        new=AsyncMock(return_value={
            "id": 1, "telegram_id": 123, "chat_id": -1001,
            "invite_link": "https://t.me/+abc", "paid_until": paid_until,
            "joined_at": None, "revoked_at": None,
        }),
    ):
        await on_join_request(request)

    request.decline.assert_awaited_once()
    request.approve.assert_not_awaited()


async def test_join_declined_when_invite_link_unknown():
    request = _make_request(user_id=123, chat_id=-1001, invite_link_url="https://t.me/+unknown")

    with patch(
        "app.bot.handlers.join_request.find_granted_by_link",
        new=AsyncMock(return_value=None),
    ):
        await on_join_request(request)

    request.decline.assert_awaited_once()
    request.approve.assert_not_awaited()


async def test_join_declined_when_no_invite_link():
    """Юзер вступил по primary-ссылке канала или другому каналу обхода —
    бот не знает откуда, поэтому decline."""
    request = _make_request(user_id=123, chat_id=-1001, invite_link_url=None)

    with patch(
        "app.bot.handlers.join_request.find_granted_by_link",
        new=AsyncMock(),
    ) as m_find:
        await on_join_request(request)

    request.decline.assert_awaited_once()
    request.approve.assert_not_awaited()
    m_find.assert_not_awaited()  # даже не дошли до БД
