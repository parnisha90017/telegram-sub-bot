"""user:reissue_links — пользовательская кнопка «Получить ссылки заново».

Проверяем:
  * happy path: revoke + update last_reissue + issue_invite_links_and_send + OK
  * rate limit: last_reissue_at < 1ч назад → отказ, SQL не зовётся
  * rate limit граница: last_reissue_at = 61 мин → проходит
  * paid_until <= NOW → MY_REISSUE_EXPIRED, SQL не зовётся
  * юзер отсутствует в users → MY_REISSUE_EXPIRED
  * issue_invite_links_and_send бросает → MY_REISSUE_ERROR + лог
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bot.handlers.start import on_reissue_links


def _make_callback(tg_id: int = 100) -> MagicMock:
    cq = MagicMock()
    cq.from_user = MagicMock()
    cq.from_user.id = tg_id
    cq.message = MagicMock()
    cq.answer = AsyncMock()
    return cq


def _make_bot() -> MagicMock:
    return MagicMock()


async def test_reissue_revokes_old_and_issues_new():
    """Happy path: paid_until в будущем, last_reissue_at старый → SQL revoke +
    UPDATE + issue_invite_links_and_send + answer MY_REISSUE_OK."""
    paid_until = datetime.now(timezone.utc) + timedelta(days=5)
    old_reissue = datetime.now(timezone.utc) - timedelta(hours=2)

    with patch(
        "app.bot.handlers.start.get_reissue_status",
        new=AsyncMock(return_value=(paid_until, old_reissue)),
    ), patch(
        "app.bot.handlers.start.perform_user_reissue_atomic",
        new=AsyncMock(return_value=4),
    ) as m_atomic, patch(
        "app.bot.handlers.start.issue_invite_links_and_send",
        new=AsyncMock(),
    ) as m_issue:
        cq = _make_callback(100)
        bot = _make_bot()
        await on_reissue_links(cq, bot)

    m_atomic.assert_awaited_once_with(100)
    m_issue.assert_awaited_once_with(bot, 100, paid_until)
    cq.answer.assert_awaited_once()
    text = cq.answer.await_args.args[0]
    assert "Новые ссылки отправлены" in text


async def test_reissue_rate_limited():
    """last_reissue_at = 30 мин назад → отказ, SQL и issue не зовутся."""
    paid_until = datetime.now(timezone.utc) + timedelta(days=5)
    recent_reissue = datetime.now(timezone.utc) - timedelta(minutes=30)

    with patch(
        "app.bot.handlers.start.get_reissue_status",
        new=AsyncMock(return_value=(paid_until, recent_reissue)),
    ), patch(
        "app.bot.handlers.start.perform_user_reissue_atomic",
        new=AsyncMock(return_value=4),
    ) as m_atomic, patch(
        "app.bot.handlers.start.issue_invite_links_and_send",
        new=AsyncMock(),
    ) as m_issue:
        cq = _make_callback(100)
        bot = _make_bot()
        await on_reissue_links(cq, bot)

    m_atomic.assert_not_awaited()
    m_issue.assert_not_awaited()
    cq.answer.assert_awaited_once()
    text = cq.answer.await_args.args[0]
    assert "слишком часты" in text.lower() or "часты" in text.lower()
    # Проверяем что подставилось число минут (около 30)
    assert "мин" in text


async def test_reissue_just_passed_rate_limit():
    """last_reissue_at = 61 мин назад → проходит, как и > 1ч."""
    paid_until = datetime.now(timezone.utc) + timedelta(days=5)
    just_past = datetime.now(timezone.utc) - timedelta(minutes=61)

    with patch(
        "app.bot.handlers.start.get_reissue_status",
        new=AsyncMock(return_value=(paid_until, just_past)),
    ), patch(
        "app.bot.handlers.start.perform_user_reissue_atomic",
        new=AsyncMock(return_value=2),
    ) as m_atomic, patch(
        "app.bot.handlers.start.issue_invite_links_and_send",
        new=AsyncMock(),
    ) as m_issue:
        cq = _make_callback(100)
        bot = _make_bot()
        await on_reissue_links(cq, bot)

    m_atomic.assert_awaited_once()
    m_issue.assert_awaited_once()


async def test_reissue_expired_user():
    """paid_until < NOW → MY_REISSUE_EXPIRED, SQL не зовётся."""
    expired = datetime.now(timezone.utc) - timedelta(hours=1)

    with patch(
        "app.bot.handlers.start.get_reissue_status",
        new=AsyncMock(return_value=(expired, None)),
    ), patch(
        "app.bot.handlers.start.perform_user_reissue_atomic",
        new=AsyncMock(return_value=0),
    ) as m_atomic, patch(
        "app.bot.handlers.start.issue_invite_links_and_send",
        new=AsyncMock(),
    ) as m_issue:
        cq = _make_callback(100)
        bot = _make_bot()
        await on_reissue_links(cq, bot)

    m_atomic.assert_not_awaited()
    m_issue.assert_not_awaited()
    cq.answer.assert_awaited_once()
    text = cq.answer.await_args.args[0]
    assert "истекла" in text.lower()


async def test_reissue_unknown_user():
    """Юзера нет в users (get_reissue_status возвращает (None, None)) →
    MY_REISSUE_EXPIRED."""
    with patch(
        "app.bot.handlers.start.get_reissue_status",
        new=AsyncMock(return_value=(None, None)),
    ), patch(
        "app.bot.handlers.start.perform_user_reissue_atomic",
        new=AsyncMock(return_value=0),
    ) as m_atomic, patch(
        "app.bot.handlers.start.issue_invite_links_and_send",
        new=AsyncMock(),
    ) as m_issue:
        cq = _make_callback(999)
        bot = _make_bot()
        await on_reissue_links(cq, bot)

    m_atomic.assert_not_awaited()
    m_issue.assert_not_awaited()
    cq.answer.assert_awaited_once()
    text = cq.answer.await_args.args[0]
    assert "истекла" in text.lower()


async def test_reissue_issue_fails(caplog):
    """issue_invite_links_and_send бросает (например, бот потерял права) →
    revoke уже сделан, юзеру MY_REISSUE_ERROR, ошибка залогирована."""
    import logging

    paid_until = datetime.now(timezone.utc) + timedelta(days=5)
    old_reissue = datetime.now(timezone.utc) - timedelta(hours=2)

    with patch(
        "app.bot.handlers.start.get_reissue_status",
        new=AsyncMock(return_value=(paid_until, old_reissue)),
    ), patch(
        "app.bot.handlers.start.perform_user_reissue_atomic",
        new=AsyncMock(return_value=4),
    ) as m_atomic, patch(
        "app.bot.handlers.start.issue_invite_links_and_send",
        new=AsyncMock(side_effect=RuntimeError("bot lost rights")),
    ) as m_issue, caplog.at_level(logging.ERROR, logger="app.bot.handlers.start"):
        cq = _make_callback(100)
        bot = _make_bot()
        await on_reissue_links(cq, bot)

    m_atomic.assert_awaited_once_with(100)
    m_issue.assert_awaited_once()
    cq.answer.assert_awaited_once()
    text = cq.answer.await_args.args[0]
    assert "не удалось" in text.lower() or "поддержку" in text.lower()
    # Ошибка попала в лог
    assert any(
        "reissue" in r.message.lower() and "tg=100" in r.message
        for r in caplog.records
    ), f"expected ERROR log about reissue failure, got: {[r.message for r in caplog.records]}"
