"""kick_expired v3 — учитывает source.

Покрытие:
  * legacy_import + expired → кик
  * legacy_import + active → не кик
  * invite_link + joined_at NOT NULL + expired → кик (как было)
  * invite_link + joined_at NULL + expired → НЕ кик (как было)

Сама логика фильтрации — внутри SQL list_expired_granted; здесь мокаем
её результат и проверяем что kick_expired корректно обрабатывает строки
с разным source.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.scheduler.jobs import kick_expired


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    return bot


async def test_kick_handles_legacy_import_rows():
    """SELECT возвращает ровно legacy_import-запись (без invite_link) —
    kick должен сработать."""
    bot = _make_bot()
    granted_rows = [
        {
            "id": 11, "telegram_id": 100, "chat_id": -1001,
            "invite_link": None, "source": "legacy_import",
        },
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

    bot.ban_chat_member.assert_awaited_once_with(chat_id=-1001, user_id=100)
    m_revoke.assert_awaited_once_with(11)


async def test_kick_handles_invite_link_rows_with_source():
    bot = _make_bot()
    granted_rows = [
        {
            "id": 22, "telegram_id": 200, "chat_id": -1002,
            "invite_link": "https://t.me/+aaa", "source": "invite_link",
        },
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

    bot.ban_chat_member.assert_awaited_once_with(chat_id=-1002, user_id=200)
    m_revoke.assert_awaited_once_with(22)


async def test_kick_mixed_sources():
    """Список содержит и legacy_import, и invite_link — каждый кикается
    по своему telegram_id и chat_id."""
    bot = _make_bot()
    granted_rows = [
        {"id": 1, "telegram_id": 100, "chat_id": -1001,
         "invite_link": None, "source": "legacy_import"},
        {"id": 2, "telegram_id": 200, "chat_id": -1002,
         "invite_link": "https://t.me/+aaa", "source": "invite_link"},
        {"id": 3, "telegram_id": 300, "chat_id": -1003,
         "invite_link": None, "source": "legacy_import"},
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

    assert bot.ban_chat_member.await_count == 3
    bans = {(c.kwargs["chat_id"], c.kwargs["user_id"]) for c in bot.ban_chat_member.await_args_list}
    assert bans == {(-1001, 100), (-1002, 200), (-1003, 300)}
    revoked_ids = {c.args[0] for c in m_revoke.await_args_list}
    assert revoked_ids == {1, 2, 3}


async def test_kick_select_filters_pending_invite_link():
    """Регрессия SQL: list_expired_granted НЕ должна возвращать
    invite_link-записи без joined_at. Здесь проверяем сам SQL через
    моковый pool — реальной БД нет, но проверяем что в query явно
    есть фильтр source/joined_at."""
    from app.db.queries import list_expired_granted

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    with patch("app.db.queries.get_pool", return_value=pool):
        await list_expired_granted(limit=100)

    sql = pool.fetch.await_args.args[0]
    assert "source" in sql
    assert "legacy_import" in sql
    assert "joined_at IS NOT NULL" in sql
    assert "revoked_at IS NULL" in sql
