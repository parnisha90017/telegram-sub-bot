"""Tests for the 8 admin commands.

Strategy:
  * Replace `app.bot.handlers.admin.get_pool` with a hand-rolled FakePool
    whose `.fetchrow` / `.fetch` / `.fetchval` / `.execute` are AsyncMocks.
  * Set `settings.admin_telegram_id` via monkeypatch so admin_only allows
    our test user. For "non-admin" cases use a different from_user.id.
  * Build minimal MagicMock for Message / CommandObject / Bot.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot.handlers.admin import (
    cmd_export, cmd_extend, cmd_find, cmd_grant, cmd_health, cmd_import_legacy,
    cmd_pending, cmd_reduce, cmd_reissue, cmd_revoke, cmd_stats,
    cmd_cleanup_chats,
)
from app.db.queries import ImportLegacyResult, ReduceResult


ADMIN_ID = 555


@pytest.fixture(autouse=True)
def _set_admin(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.settings.admin_telegram_id", ADMIN_ID)


@pytest.fixture(autouse=True)
def _stub_breakdowns(monkeypatch):
    """Заглушки на breakdown-функции, которые добавлены к /find /health.
    Тесты, которым важны эти данные, переопределяют через monkeypatch.setattr
    повторно (последняя установка побеждает)."""
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_user_access_breakdown",
        AsyncMock(return_value={"via_invite": 0, "via_legacy": 0}),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_active_breakdown",
        AsyncMock(return_value={
            "active_users": 0, "via_invite": 0,
            "via_legacy": 0, "legacy_no_import": 0,
        }),
    )


def _make_msg(from_user_id: int = ADMIN_ID) -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = from_user_id
    msg.answer = AsyncMock()
    msg.answer_document = AsyncMock()
    return msg


def _make_command(args: str | None) -> MagicMock:
    cmd = MagicMock()
    cmd.args = args
    return cmd


def _make_pool() -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=0)
    pool.execute = AsyncMock()
    return pool


# -----------------------------------------------------------------------------
# admin_only
# -----------------------------------------------------------------------------

async def test_non_admin_silently_ignored():
    """Юзер с не-admin tg_id шлёт /find — handler ничего не отвечает."""
    msg = _make_msg(from_user_id=999)  # не админ
    cmd = _make_command(args="123")
    await cmd_find(msg, command=cmd)
    msg.answer.assert_not_awaited()


async def test_admin_id_zero_blocks_everyone(monkeypatch):
    """settings.admin_telegram_id=0 → даже юзер с id=0 не получает доступ."""
    monkeypatch.setattr("app.bot.handlers.admin.settings.admin_telegram_id", 0)
    msg = _make_msg(from_user_id=0)
    cmd = _make_command(args="123")
    await cmd_find(msg, command=cmd)
    msg.answer.assert_not_awaited()


# -----------------------------------------------------------------------------
# /find
# -----------------------------------------------------------------------------

async def test_find_by_telegram_id(monkeypatch):
    pool = _make_pool()
    pool.fetchrow = AsyncMock(side_effect=[
        {
            "telegram_id": 123, "username": "alice", "plan": "tariff_3d",
            "paid_until": datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            "status": "active",
            "created_at": datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc),
        },
    ])
    pool.fetch = AsyncMock(side_effect=[
        # payments
        [{
            "plan": "tariff_3d", "amount": Decimal("11"), "status": "paid",
            "provider": "cryptobot",
            "created_at": datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc),
        }],
        # granted_access
        [{
            "chat_id": -1001, "paid_until": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "joined_at": datetime(2026, 4, 28, tzinfo=timezone.utc),
            "revoked_at": None,
        }],
    ])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.is_legacy_active_user",
        AsyncMock(return_value=False),
    )

    msg = _make_msg()
    cmd = _make_command(args="123")
    await cmd_find(msg, command=cmd)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "123" in text
    assert "alice" in text
    assert "01.05.2026" in text  # paid_until
    assert "tariff_3d" in text
    assert "✅ вступил" in text


async def test_find_by_username_case_insensitive(monkeypatch):
    pool = _make_pool()
    user_row = {
        "telegram_id": 123, "username": "Alice", "plan": "tariff_7d",
        "paid_until": datetime(2026, 5, 1, tzinfo=timezone.utc), "status": "active",
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    }
    pool.fetchrow = AsyncMock(return_value=user_row)
    pool.fetch = AsyncMock(side_effect=[[], []])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.is_legacy_active_user",
        AsyncMock(return_value=False),
    )

    msg = _make_msg()
    cmd = _make_command(args="@alice")
    await cmd_find(msg, command=cmd)

    msg.answer.assert_awaited_once()
    # SELECT прошёл с LOWER(...)
    sql, _arg = pool.fetchrow.await_args.args[0], pool.fetchrow.await_args.args[1]
    assert "LOWER(username)" in sql


async def test_find_user_not_found(monkeypatch):
    pool = _make_pool()
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    cmd = _make_command(args="999999")
    await cmd_find(msg, command=cmd)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "не найден" in text.lower()


async def test_find_no_args_shows_usage():
    msg = _make_msg()
    cmd = _make_command(args=None)
    await cmd_find(msg, command=cmd)
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Usage" in text


# -----------------------------------------------------------------------------
# /export
# -----------------------------------------------------------------------------

async def test_export_generates_xlsx(monkeypatch):
    pool = _make_pool()
    pool.fetch = AsyncMock(return_value=[
        {
            "telegram_id": 1, "username": "alice", "plan": "tariff_3d",
            "paid_until": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "status": "active",
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "total_paid": Decimal("11"), "payment_count": 1,
            "last_payment": datetime(2026, 4, 28, tzinfo=timezone.utc),
            "last_provider": "cryptobot", "active_chats": 4,
        },
        {
            "telegram_id": 2, "username": None, "plan": None,
            "paid_until": None, "status": "new",
            "created_at": datetime(2026, 4, 23, tzinfo=timezone.utc),
            "total_paid": Decimal("0"), "payment_count": 0,
            "last_payment": None, "last_provider": None, "active_chats": 0,
        },
    ])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    await cmd_export(msg)

    msg.answer_document.assert_awaited_once()
    args, kwargs = msg.answer_document.await_args
    doc = args[0]
    # filename .xlsx
    assert doc.filename.endswith(".xlsx")
    # бинарный xlsx с правильной сигнатурой (PK\x03\x04 — zip header)
    assert doc.data[:4] == b"PK\x03\x04"
    # 2 юзера в caption
    caption = kwargs.get("caption") or (args[1] if len(args) > 1 else "")
    assert "2" in caption


# -----------------------------------------------------------------------------
# /stats
# -----------------------------------------------------------------------------

async def test_stats_returns_correct_counts(monkeypatch):
    pool = _make_pool()
    pool.fetchrow = AsyncMock(side_effect=[
        # user_stats
        {"total": 10, "active": 6, "expired": 3, "new_users": 1},
        # revenue
        {"day": Decimal("21"), "week": Decimal("100"), "month": Decimal("500"),
         "total": Decimal("1234.56")},
    ])
    pool.fetch = AsyncMock(side_effect=[
        # plan_stats
        [{"plan": "tariff_7d", "cnt": 4}, {"plan": "tariff_3d", "cnt": 2}],
        # by_provider
        [{"provider": "cryptobot", "cnt": 8, "revenue": Decimal("800")},
         {"provider": "heleket", "cnt": 2, "revenue": Decimal("434.56")}],
    ])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    await cmd_stats(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Всего: 10" in text
    assert "Активных: 6" in text
    assert "Просрочено: 3" in text
    assert "tariff_7d: 4" in text
    assert "1234.56" in text
    assert "cryptobot: 8" in text
    assert "heleket: 2" in text


# -----------------------------------------------------------------------------
# /extend
# -----------------------------------------------------------------------------

async def test_extend_adds_days(monkeypatch):
    new_paid_until = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    pool = _make_pool()
    pool.fetchval = AsyncMock(return_value=new_paid_until)
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    cmd = _make_command(args="123 7")
    await cmd_extend(msg, command=cmd)

    # 1) INSERT users ON CONFLICT DO NOTHING — execute
    # 2) UPDATE users RETURNING paid_until — fetchval
    # 3) UPDATE granted_access — execute
    assert pool.execute.await_count == 2
    pool.fetchval.assert_awaited_once()
    fetchval_sql = pool.fetchval.await_args.args[0]
    assert "GREATEST" in fetchval_sql
    assert "make_interval" in fetchval_sql

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "123" in text
    assert "7 дн" in text
    assert "12.05.2026" in text


async def test_extend_creates_user_if_not_exists(monkeypatch):
    pool = _make_pool()
    pool.fetchval = AsyncMock(return_value=datetime(2026, 5, 23, tzinfo=timezone.utc))
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    cmd = _make_command(args="999 30")
    await cmd_extend(msg, command=cmd)

    # Первый execute — INSERT users ON CONFLICT DO NOTHING.
    first_call_sql = pool.execute.await_args_list[0].args[0]
    assert "INSERT INTO users" in first_call_sql
    assert "ON CONFLICT" in first_call_sql


async def test_extend_rejects_bad_days(monkeypatch):
    pool = _make_pool()
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    cmd = _make_command(args="123 -5")
    await cmd_extend(msg, command=cmd)

    pool.execute.assert_not_awaited()
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "1 and 365" in text or "between" in text


async def test_extend_rejects_non_numeric():
    msg = _make_msg()
    cmd = _make_command(args="abc def")
    await cmd_extend(msg, command=cmd)
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "must be numbers" in text or "Bad input" in text


# -----------------------------------------------------------------------------
# /revoke
# -----------------------------------------------------------------------------

async def test_revoke_kicks_from_all_active_chats(monkeypatch):
    pool = _make_pool()
    pool.fetch = AsyncMock(return_value=[
        {"id": 11, "chat_id": -1001},
        {"id": 12, "chat_id": -1002},
    ])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()

    msg = _make_msg()
    cmd = _make_command(args="123")
    await cmd_revoke(msg, command=cmd, bot=bot)

    assert bot.ban_chat_member.await_count == 2
    assert bot.unban_chat_member.await_count == 2
    # 2 UPDATE granted_access (по одному на запись) + 1 UPDATE users (status=expired)
    assert pool.execute.await_count == 3
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Кикнут из 2" in text
    assert "Ошибок: 0" in text


async def test_revoke_handles_kick_failure(monkeypatch):
    pool = _make_pool()
    pool.fetch = AsyncMock(return_value=[
        {"id": 11, "chat_id": -1001},
        {"id": 12, "chat_id": -1002},
    ])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock(side_effect=[None, Exception("permission denied")])
    bot.unban_chat_member = AsyncMock()

    msg = _make_msg()
    cmd = _make_command(args="123")
    await cmd_revoke(msg, command=cmd, bot=bot)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Кикнут из 1" in text
    assert "Ошибок: 1" in text


# -----------------------------------------------------------------------------
# /pending
# -----------------------------------------------------------------------------

async def test_pending_shows_only_old_pending(monkeypatch):
    pool = _make_pool()
    pool.fetch = AsyncMock(return_value=[
        {
            "id": 5, "telegram_id": 100, "plan": "tariff_3d",
            "amount": Decimal("11"), "provider": "heleket",
            "created_at": datetime.now(timezone.utc) - timedelta(hours=3),
            "age_minutes": 180.0,
        },
    ])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    await cmd_pending(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "1 pending" in text
    assert "tariff_3d" in text
    assert "heleket" in text


async def test_pending_empty(monkeypatch):
    pool = _make_pool()
    pool.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    msg = _make_msg()
    await cmd_pending(msg)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Нет зависших" in text


# -----------------------------------------------------------------------------
# /health
# -----------------------------------------------------------------------------

async def test_health_reports_db_ok_and_chats(monkeypatch):
    pool = _make_pool()
    pool.fetchval = AsyncMock(side_effect=[
        1,   # SELECT 1 (db check)
        0,   # pending count
        7,   # active granted_access
    ])
    pool.fetchrow = AsyncMock(return_value={
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=15),
        "provider": "cryptobot",
    })
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_legacy_active_users",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.chat_ids",
        [-1001, -1002, -1003, -1004],
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.enabled_providers",
        ["cryptobot", "heleket"],
    )

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(return_value=42)

    msg = _make_msg()
    await cmd_health(msg, bot=bot)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Database: ok" in text
    assert "cryptobot" in text and "heleket" in text
    assert "Все 4 каналов" in text  # все доступны
    assert "Активных granted_access: 7" in text


async def test_health_reports_pending_warning(monkeypatch):
    pool = _make_pool()
    pool.fetchval = AsyncMock(side_effect=[1, 5, 0])  # 5 зависших pending
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_legacy_active_users",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr("app.bot.handlers.admin.settings.chat_ids", [])
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.enabled_providers", ["cryptobot"],
    )

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock()

    msg = _make_msg()
    await cmd_health(msg, bot=bot)

    text = msg.answer.await_args.args[0]
    assert "Зависших pending: 5" in text


# -----------------------------------------------------------------------------
# /cleanup_chats
# -----------------------------------------------------------------------------

async def test_cleanup_chats_explains_limitation(monkeypatch):
    pool = _make_pool()
    pool.fetchval = AsyncMock(side_effect=[7, 5])  # active_granted, active_users
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.chat_ids", [-1001, -1002],
    )

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock(side_effect=[100, 80])
    chat_obj_1 = MagicMock(); chat_obj_1.title = "Chat A"
    chat_obj_2 = MagicMock(); chat_obj_2.title = "Chat B"
    bot.get_chat = AsyncMock(side_effect=[chat_obj_1, chat_obj_2])

    msg = _make_msg()
    await cmd_cleanup_chats(msg, bot=bot)

    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "Bot API" in text
    assert "Chat A" in text and "100" in text
    assert "Chat B" in text and "80" in text
    assert "granted_access: 7" in text
    assert "подписок (users): 5" in text


# -----------------------------------------------------------------------------
# /reduce
# -----------------------------------------------------------------------------

async def test_reduce_active_user_no_kick(monkeypatch):
    """Юзер найден, новый срок ещё в будущем — без kick'ов."""
    new_until = datetime(2026, 5, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.bot.handlers.admin.reduce_subscription",
        AsyncMock(return_value=ReduceResult(
            found=True, new_paid_until=new_until, now_expired=False,
            active_granted=[],
        )),
    )

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    msg = _make_msg()
    cmd = _make_command(args="123 5")
    await cmd_reduce(msg, command=cmd, bot=bot)

    bot.ban_chat_member.assert_not_awaited()
    bot.unban_chat_member.assert_not_awaited()
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "123" in text
    assert "5 дн" in text
    assert "active" in text.lower()
    assert "01.05.2026" in text


async def test_reduce_pushes_user_to_expired_kicks_all_chats(monkeypatch):
    """Подписка ушла в прошлое — кикаем всех из active_granted и
    помечаем revoked_at для каждой ссылки."""
    new_until = datetime(2026, 4, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.bot.handlers.admin.reduce_subscription",
        AsyncMock(return_value=ReduceResult(
            found=True, new_paid_until=new_until, now_expired=True,
            active_granted=[
                (-1001, "https://t.me/+aaa"),
                (-1002, "https://t.me/+bbb"),
                (-1003, "https://t.me/+ccc"),
            ],
        )),
    )
    m_revoke = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.mark_revoked_by_link", m_revoke)

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    bot.unban_chat_member = AsyncMock()
    msg = _make_msg()
    cmd = _make_command(args="123 30")  # валидное значение; expired задан в моке
    await cmd_reduce(msg, command=cmd, bot=bot)

    assert bot.ban_chat_member.await_count == 3
    assert bot.unban_chat_member.await_count == 3
    # Все 3 ссылки помечены revoked_at
    revoked_links = {c.args[0] for c in m_revoke.await_args_list}
    assert revoked_links == {
        "https://t.me/+aaa", "https://t.me/+bbb", "https://t.me/+ccc",
    }
    text = msg.answer.await_args.args[0]
    assert "expired" in text.lower()
    assert "Кикнут из 3" in text


async def test_reduce_user_not_found(monkeypatch):
    m_reduce = AsyncMock(return_value=ReduceResult(found=False))
    monkeypatch.setattr("app.bot.handlers.admin.reduce_subscription", m_reduce)
    m_revoke = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.mark_revoked_by_link", m_revoke)

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock()
    msg = _make_msg()
    cmd = _make_command(args="999999 7")
    await cmd_reduce(msg, command=cmd, bot=bot)

    m_reduce.assert_awaited_once()
    bot.ban_chat_member.assert_not_awaited()
    m_revoke.assert_not_awaited()
    text = msg.answer.await_args.args[0]
    assert "не найден" in text.lower()


async def test_reduce_no_args_shows_usage():
    msg = _make_msg()
    bot = MagicMock()
    cmd = _make_command(args=None)
    await cmd_reduce(msg, command=cmd, bot=bot)
    text = msg.answer.await_args.args[0]
    assert "Usage" in text or "/reduce" in text


async def test_reduce_rejects_bad_days(monkeypatch):
    m_reduce = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.reduce_subscription", m_reduce)
    msg = _make_msg()
    bot = MagicMock()
    cmd = _make_command(args="123 -5")
    await cmd_reduce(msg, command=cmd, bot=bot)
    m_reduce.assert_not_awaited()
    text = msg.answer.await_args.args[0]
    assert "1 and 365" in text or "between" in text


async def test_reduce_rejects_non_numeric(monkeypatch):
    m_reduce = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.reduce_subscription", m_reduce)
    msg = _make_msg()
    bot = MagicMock()
    cmd = _make_command(args="abc def")
    await cmd_reduce(msg, command=cmd, bot=bot)
    m_reduce.assert_not_awaited()


async def test_reduce_handles_telegram_bad_request_silently(monkeypatch):
    """Юзер уже не в чате (TelegramBadRequest) — глотаем тихо, не считаем
    кик ошибкой, помечаем revoked всё равно."""
    from aiogram.exceptions import TelegramBadRequest

    new_until = datetime(2026, 4, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.bot.handlers.admin.reduce_subscription",
        AsyncMock(return_value=ReduceResult(
            found=True, new_paid_until=new_until, now_expired=True,
            active_granted=[(-1001, "https://t.me/+aaa")],
        )),
    )
    m_revoke = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.mark_revoked_by_link", m_revoke)

    bot = MagicMock()
    bot.ban_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=MagicMock(), message="user not found")
    )
    bot.unban_chat_member = AsyncMock()
    msg = _make_msg()
    cmd = _make_command(args="123 30")
    await cmd_reduce(msg, command=cmd, bot=bot)

    # mark_revoked всё равно вызвана (трактуем BadRequest как «уже не в чате»).
    m_revoke.assert_awaited_once_with("https://t.me/+aaa")


# -----------------------------------------------------------------------------
# /find — legacy-флаг
# -----------------------------------------------------------------------------

async def test_find_shows_legacy_flag_when_active_without_joined(monkeypatch):
    pool = _make_pool()
    pool.fetchrow = AsyncMock(side_effect=[
        {
            "telegram_id": 777, "username": "legacy_user",
            "plan": "tariff_30d",
            "paid_until": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "status": "active",
            "created_at": datetime(2025, 12, 1, tzinfo=timezone.utc),
        },
    ])
    pool.fetch = AsyncMock(side_effect=[[], []])  # payments + granted
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.is_legacy_active_user",
        AsyncMock(return_value=True),
    )

    msg = _make_msg()
    cmd = _make_command(args="777")
    await cmd_find(msg, command=cmd)

    text = msg.answer.await_args.args[0]
    assert "Legacy" in text or "legacy" in text.lower()
    assert "anti-share" in text.lower() or "до-фикса" in text.lower()


async def test_find_no_legacy_flag_when_user_joined(monkeypatch):
    pool = _make_pool()
    pool.fetchrow = AsyncMock(side_effect=[
        {
            "telegram_id": 100, "username": "normal", "plan": "tariff_3d",
            "paid_until": datetime(2026, 5, 1, tzinfo=timezone.utc),
            "status": "active",
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        },
    ])
    pool.fetch = AsyncMock(side_effect=[[], []])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.is_legacy_active_user",
        AsyncMock(return_value=False),
    )

    msg = _make_msg()
    cmd = _make_command(args="100")
    await cmd_find(msg, command=cmd)

    text = msg.answer.await_args.args[0]
    assert "Legacy" not in text
    assert "anti-share" not in text


# -----------------------------------------------------------------------------
# /health — legacy-счётчик
# -----------------------------------------------------------------------------

async def test_health_reports_legacy_count(monkeypatch):
    pool = _make_pool()
    pool.fetchval = AsyncMock(side_effect=[1, 0, 5])  # db ok / pending=0 / granted=5
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_legacy_active_users",
        AsyncMock(return_value=12),
    )
    # legacy_no_import должен совпадать с count_legacy_active_users (12),
    # иначе появится diagnostic-строка про расхождение.
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_active_breakdown",
        AsyncMock(return_value={
            "active_users": 20, "via_invite": 5,
            "via_legacy": 3, "legacy_no_import": 12,
        }),
    )
    monkeypatch.setattr("app.bot.handlers.admin.settings.chat_ids", [])
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.enabled_providers", ["cryptobot"],
    )

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock()
    msg = _make_msg()
    await cmd_health(msg, bot=bot)

    text = msg.answer.await_args.args[0]
    # Новый формат: «Legacy без импорта: N» в блоке breakdown
    assert "Legacy без импорта: 12" in text
    assert "12 юзеров не будут кикнуты" in text


async def test_health_legacy_zero_shown_as_ok(monkeypatch):
    pool = _make_pool()
    pool.fetchval = AsyncMock(side_effect=[1, 0, 5])
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_legacy_active_users",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_active_breakdown",
        AsyncMock(return_value={
            "active_users": 5, "via_invite": 5,
            "via_legacy": 0, "legacy_no_import": 0,
        }),
    )
    monkeypatch.setattr("app.bot.handlers.admin.settings.chat_ids", [])
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.enabled_providers", ["cryptobot"],
    )

    bot = MagicMock()
    bot.get_chat_member_count = AsyncMock()
    msg = _make_msg()
    await cmd_health(msg, bot=bot)

    text = msg.answer.await_args.args[0]
    assert "Legacy без импорта: 0" in text
    # alert-строка не должна появиться при 0
    assert "не будут кикнуты" not in text


# -----------------------------------------------------------------------------
# /import_legacy
# -----------------------------------------------------------------------------

async def test_import_legacy_happy_path(monkeypatch):
    fake_until = datetime(2026, 6, 1, tzinfo=timezone.utc)
    m_import = AsyncMock(return_value=ImportLegacyResult(
        new_paid_until=fake_until, was_created=True, granted_count=4,
    ))
    monkeypatch.setattr("app.bot.handlers.admin.import_legacy_user", m_import)
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.chat_ids", [-1, -2, -3, -4],
    )

    bot = MagicMock()
    msg = _make_msg()
    cmd = _make_command(args="123 30")
    await cmd_import_legacy(msg, command=cmd, bot=bot)

    m_import.assert_awaited_once_with(123, 30, [-1, -2, -3, -4])
    text = msg.answer.await_args.args[0]
    assert "Импортирован" in text
    assert "123" in text
    assert "Granted_access: 4" in text
    assert "да" in text  # was_created


async def test_import_legacy_resolves_username(monkeypatch):
    fake_until = datetime(2026, 6, 1, tzinfo=timezone.utc)
    m_import = AsyncMock(return_value=ImportLegacyResult(
        new_paid_until=fake_until, was_created=False, granted_count=4,
    ))
    monkeypatch.setattr("app.bot.handlers.admin.import_legacy_user", m_import)
    monkeypatch.setattr(
        "app.bot.handlers.admin.settings.chat_ids", [-1, -2, -3, -4],
    )

    bot = MagicMock()
    chat = MagicMock(); chat.id = 999
    bot.get_chat = AsyncMock(return_value=chat)
    msg = _make_msg()
    cmd = _make_command(args="@petrov 15")
    await cmd_import_legacy(msg, command=cmd, bot=bot)

    bot.get_chat.assert_awaited_once_with("@petrov")
    m_import.assert_awaited_once_with(999, 15, [-1, -2, -3, -4])


async def test_import_legacy_invalid_days(monkeypatch):
    m_import = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.import_legacy_user", m_import)
    bot = MagicMock()
    msg = _make_msg()
    await cmd_import_legacy(msg, command=_make_command(args="123 -5"), bot=bot)
    m_import.assert_not_awaited()


async def test_import_legacy_no_args():
    bot = MagicMock()
    msg = _make_msg()
    await cmd_import_legacy(msg, command=_make_command(args=None), bot=bot)
    text = msg.answer.await_args.args[0]
    assert "Usage" in text or "import_legacy" in text


# -----------------------------------------------------------------------------
# /grant
# -----------------------------------------------------------------------------

async def test_grant_happy_path(monkeypatch):
    fake_until = datetime(2026, 5, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.bot.handlers.admin._extend_user",
        AsyncMock(return_value="✅ extended"),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.get_user_paid_until",
        AsyncMock(return_value=fake_until),
    )
    m_grant = AsyncMock(return_value=(["https://t.me/+a", "https://t.me/+b"], True))
    monkeypatch.setattr("app.bot.handlers.admin._grant_access_to_user", m_grant)

    bot = MagicMock()
    msg = _make_msg()
    cmd = _make_command(args="123 7")
    await cmd_grant(msg, command=cmd, bot=bot)

    m_grant.assert_awaited_once_with(bot, 123, fake_until)
    text = msg.answer.await_args.args[0]
    assert "Выдан доступ" in text
    assert "ЛС юзера" in text


async def test_grant_forbidden_falls_back_to_admin(monkeypatch):
    fake_until = datetime(2026, 5, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "app.bot.handlers.admin._extend_user",
        AsyncMock(return_value="✅ extended"),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.get_user_paid_until",
        AsyncMock(return_value=fake_until),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin._grant_access_to_user",
        AsyncMock(return_value=(["https://t.me/+a", "https://t.me/+b"], False)),
    )

    bot = MagicMock()
    msg = _make_msg()
    cmd = _make_command(args="123 7")
    await cmd_grant(msg, command=cmd, bot=bot)

    text = msg.answer.await_args.args[0]
    assert "Юзер ещё не писал боту" in text or "ЛС недоступно" in text
    assert "https://t.me/+a" in text
    assert "https://t.me/+b" in text


# -----------------------------------------------------------------------------
# /reissue
# -----------------------------------------------------------------------------

async def test_reissue_happy_path(monkeypatch):
    fake_until = datetime.now(timezone.utc) + timedelta(days=10)
    monkeypatch.setattr(
        "app.bot.handlers.admin.get_user_paid_until",
        AsyncMock(return_value=fake_until),
    )
    m_revoke = AsyncMock(return_value=4)
    monkeypatch.setattr("app.bot.handlers.admin.revoke_all_active_for_user", m_revoke)
    monkeypatch.setattr(
        "app.bot.handlers.admin._grant_access_to_user",
        AsyncMock(return_value=(["L1", "L2", "L3", "L4"], True)),
    )

    bot = MagicMock()
    msg = _make_msg()
    cmd = _make_command(args="555")
    await cmd_reissue(msg, command=cmd, bot=bot)

    m_revoke.assert_awaited_once_with(555)
    text = msg.answer.await_args.args[0]
    assert "Перевыдано 4" in text
    assert "555" in text


async def test_reissue_expired_user(monkeypatch):
    """Юзер с истёкшей подпиской → возвращаем REISSUE_EXPIRED, revoke не вызван."""
    monkeypatch.setattr(
        "app.bot.handlers.admin.get_user_paid_until",
        AsyncMock(return_value=datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    m_revoke = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.revoke_all_active_for_user", m_revoke)
    m_grant = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin._grant_access_to_user", m_grant)

    bot = MagicMock()
    msg = _make_msg()
    await cmd_reissue(msg, command=_make_command(args="555"), bot=bot)

    m_revoke.assert_not_awaited()
    m_grant.assert_not_awaited()
    text = msg.answer.await_args.args[0]
    assert "истекла" in text


async def test_reissue_user_not_in_db(monkeypatch):
    monkeypatch.setattr(
        "app.bot.handlers.admin.get_user_paid_until",
        AsyncMock(return_value=None),
    )
    m_revoke = AsyncMock()
    monkeypatch.setattr("app.bot.handlers.admin.revoke_all_active_for_user", m_revoke)

    bot = MagicMock()
    msg = _make_msg()
    await cmd_reissue(msg, command=_make_command(args="999999"), bot=bot)

    m_revoke.assert_not_awaited()
    text = msg.answer.await_args.args[0]
    assert "истекла" in text or "нет" in text.lower()


async def test_reissue_forbidden_falls_back_to_admin(monkeypatch):
    fake_until = datetime.now(timezone.utc) + timedelta(days=5)
    monkeypatch.setattr(
        "app.bot.handlers.admin.get_user_paid_until",
        AsyncMock(return_value=fake_until),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.revoke_all_active_for_user",
        AsyncMock(return_value=4),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin._grant_access_to_user",
        AsyncMock(return_value=(["L1", "L2"], False)),
    )

    bot = MagicMock()
    msg = _make_msg()
    await cmd_reissue(msg, command=_make_command(args="555"), bot=bot)

    text = msg.answer.await_args.args[0]
    assert "заблокировал бота" in text
    assert "L1" in text and "L2" in text


# -----------------------------------------------------------------------------
# /find — breakdown по source
# -----------------------------------------------------------------------------

async def test_find_shows_access_breakdown(monkeypatch):
    pool = _make_pool()
    pool.fetchrow = AsyncMock(return_value={
        "telegram_id": 100, "username": "u", "plan": "tariff_7d",
        "paid_until": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "status": "active",
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    })
    pool.fetch = AsyncMock(side_effect=[[], []])
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)
    monkeypatch.setattr(
        "app.bot.handlers.admin.is_legacy_active_user",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "app.bot.handlers.admin.count_user_access_breakdown",
        AsyncMock(return_value={"via_invite": 2, "via_legacy": 4}),
    )

    msg = _make_msg()
    await cmd_find(msg, command=_make_command(args="100"))

    text = msg.answer.await_args.args[0]
    assert "Доступ: 2 через бот-ссылки, 4 через legacy-импорт" in text
