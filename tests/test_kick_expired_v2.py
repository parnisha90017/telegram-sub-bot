"""Tests for the new kick_expired job: walks granted_access (joined+expired+
not-revoked), bans/unbans real chat participants, marks rows revoked."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from app.scheduler.jobs import kick_expired


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    return bot


async def test_kick_uses_granted_access_not_users():
    """kick_expired должен идти по granted_access (chat_id из строки), не по
    users.telegram_id × всем 4 чатам."""
    bot = _make_bot()

    granted_rows = [
        {"id": 11, "telegram_id": 100, "chat_id": -1001},
        {"id": 12, "telegram_id": 200, "chat_id": -1002},
    ]

    with patch(
        "app.scheduler.jobs.list_expired_granted",
        new=AsyncMock(return_value=granted_rows),
    ), patch(
        "app.scheduler.jobs.mark_revoked", new=AsyncMock(),
    ) as m_revoke, patch(
        "app.scheduler.jobs.expire_and_return_ids", new=AsyncMock(return_value=[]),
    ):
        await kick_expired(bot)

    # Каждый row → один ban + один unban (в свой chat_id)
    assert bot.ban_chat_member.await_count == 2
    assert bot.unban_chat_member.await_count == 2

    ban_calls = {(c.kwargs["chat_id"], c.kwargs["user_id"]) for c in bot.ban_chat_member.await_args_list}
    assert ban_calls == {(-1001, 100), (-1002, 200)}

    # mark_revoked для обеих
    revoked_ids = {c.args[0] for c in m_revoke.await_args_list}
    assert revoked_ids == {11, 12}


async def test_kick_skips_when_no_expired():
    """list_expired_granted возвращает [] (никто не вступал, или все уже
    revoked, или подписки активны) — kick не вызывается, но
    expire_and_return_ids всё равно зовётся (для users.status='expired')."""
    bot = _make_bot()

    with patch(
        "app.scheduler.jobs.list_expired_granted", new=AsyncMock(return_value=[]),
    ), patch(
        "app.scheduler.jobs.mark_revoked", new=AsyncMock(),
    ) as m_revoke, patch(
        "app.scheduler.jobs.expire_and_return_ids", new=AsyncMock(return_value=[]),
    ) as m_expire_users:
        await kick_expired(bot)

    bot.ban_chat_member.assert_not_awaited()
    bot.unban_chat_member.assert_not_awaited()
    m_revoke.assert_not_awaited()
    m_expire_users.assert_awaited_once()


async def test_kick_marks_revoked_after_successful_kick():
    """После успешного ban+unban для записи — mark_revoked(id)."""
    bot = _make_bot()

    with patch(
        "app.scheduler.jobs.list_expired_granted",
        new=AsyncMock(return_value=[{"id": 42, "telegram_id": 100, "chat_id": -1001}]),
    ), patch(
        "app.scheduler.jobs.mark_revoked", new=AsyncMock(),
    ) as m_revoke, patch(
        "app.scheduler.jobs.expire_and_return_ids", new=AsyncMock(return_value=[]),
    ):
        await kick_expired(bot)

    m_revoke.assert_awaited_once_with(42)


async def test_kick_treats_telegram_bad_request_as_revoked():
    """Если юзер уже не в чате (TelegramBadRequest) — mark_revoked всё равно,
    чтобы не пытаться кикать его в каждом тике бесконечно."""
    bot = _make_bot()
    bot.ban_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="user not found")
    )

    with patch(
        "app.scheduler.jobs.list_expired_granted",
        new=AsyncMock(return_value=[{"id": 42, "telegram_id": 100, "chat_id": -1001}]),
    ), patch(
        "app.scheduler.jobs.mark_revoked", new=AsyncMock(),
    ) as m_revoke, patch(
        "app.scheduler.jobs.expire_and_return_ids", new=AsyncMock(return_value=[]),
    ):
        await kick_expired(bot)

    m_revoke.assert_awaited_once_with(42)


async def test_kick_does_not_mark_revoked_on_rate_limit():
    """TelegramRetryAfter — не помечаем revoked, попробуем в следующий тик."""
    bot = _make_bot()
    bot.ban_chat_member = AsyncMock(
        side_effect=TelegramRetryAfter(method=MagicMock(), message="rate limit", retry_after=30)
    )

    with patch(
        "app.scheduler.jobs.list_expired_granted",
        new=AsyncMock(return_value=[{"id": 42, "telegram_id": 100, "chat_id": -1001}]),
    ), patch(
        "app.scheduler.jobs.mark_revoked", new=AsyncMock(),
    ) as m_revoke, patch(
        "app.scheduler.jobs.expire_and_return_ids", new=AsyncMock(return_value=[]),
    ):
        await kick_expired(bot)

    m_revoke.assert_not_awaited()
