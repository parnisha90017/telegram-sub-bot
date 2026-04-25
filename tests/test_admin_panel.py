"""Tests for /admin inline-panel: panel display, callback dispatch, FSM
flow для find/extend/revoke, confirmation для revoke, close, /cancel.

Не дублируем существующие test_admin.py (они покрывают slash-команды и
helper'ы); здесь — только новый интерфейс панели."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from app.bot.handlers.admin import (
    AdminFlow,
    cb_admin_bulk_import_confirm,
    cb_admin_bulk_import_start,
    cb_admin_close,
    cb_admin_export,
    cb_admin_extend_start,
    cb_admin_find_start,
    cb_admin_grant_start,
    cb_admin_health,
    cb_admin_import_legacy_start,
    cb_admin_pending,
    cb_admin_reduce_start,
    cb_admin_reissue_start,
    cb_admin_revoke_confirm,
    cb_admin_revoke_start,
    cb_admin_stats,
    cmd_admin,
    fsm_bulk_import_input,
    fsm_cancel_admin,
    fsm_extend_input,
    fsm_find_query,
    fsm_grant_input,
    fsm_import_legacy_input,
    fsm_reduce_input,
    fsm_reissue_input,
    fsm_revoke_id,
)


ADMIN_ID = 555


@pytest.fixture(autouse=True)
def _set_admin(monkeypatch):
    monkeypatch.setattr("app.bot.handlers.admin.settings.admin_telegram_id", ADMIN_ID)


def _make_message(from_user_id: int = ADMIN_ID, text: str | None = None) -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = from_user_id
    msg.text = text
    msg.answer = AsyncMock()
    msg.answer_document = AsyncMock()
    return msg


def _make_callback(
    from_user_id: int = ADMIN_ID,
    data: str = "admin:stats",
    has_message: bool = True,
) -> MagicMock:
    cq = MagicMock()
    cq.from_user = MagicMock()
    cq.from_user.id = from_user_id
    cq.data = data
    cq.answer = AsyncMock()
    if has_message:
        cq.message = MagicMock()
        cq.message.answer = AsyncMock()
        cq.message.answer_document = AsyncMock()
        cq.message.delete = AsyncMock()
        cq.message.edit_text = AsyncMock()
    else:
        cq.message = None
    return cq


def _make_state() -> AsyncMock:
    state = AsyncMock()
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    state.get_state = AsyncMock(return_value=None)
    return state


# =============================================================================
# /admin command (slash entry)
# =============================================================================

async def test_admin_command_shows_panel_with_keyboard():
    msg = _make_message()
    await cmd_admin(msg)

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.await_args
    text = args[0] if args else kwargs.get("text", "")
    assert "Админ-панель" in text or "Admin" in text or "панел" in text.lower()
    kb = kwargs.get("reply_markup")
    assert kb is not None
    # Проверяем callbacks: 9 кнопок (8 действий + close)
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    expected = {
        "admin:stats", "admin:find", "admin:export", "admin:extend",
        "admin:reduce", "admin:grant", "admin:reissue",
        "admin:import_legacy", "admin:bulk_import",
        "admin:revoke", "admin:cleanup", "admin:pending",
        "admin:health", "admin:close",
    }
    assert expected.issubset(set(cbs))


async def test_admin_command_silent_for_non_admin():
    msg = _make_message(from_user_id=999)
    await cmd_admin(msg)
    msg.answer.assert_not_awaited()


# =============================================================================
# Callback access control: admin_only_cb
# =============================================================================

async def test_callback_blocked_for_non_admin_alert():
    cq = _make_callback(from_user_id=999, data="admin:stats")
    with patch(
        "app.bot.handlers.admin._build_stats_text", new=AsyncMock(),
    ) as m_build:
        await cb_admin_stats(cq)

    cq.answer.assert_awaited_once()
    args, kwargs = cq.answer.await_args
    # alert «Нет доступа»
    assert kwargs.get("show_alert") is True
    # бизнес-логика НЕ вызвана
    m_build.assert_not_awaited()


async def test_callback_revoke_confirm_blocked_for_non_admin():
    """Самый критичный сценарий: revoke_confirm:<id> от не-админа НЕ должен
    кикать юзера."""
    cq = _make_callback(from_user_id=999, data="admin:revoke_confirm:123")
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._revoke_user", new=AsyncMock(),
    ) as m_revoke:
        await cb_admin_revoke_confirm(cq, bot=bot)

    m_revoke.assert_not_awaited()
    cq.answer.assert_awaited_once()
    assert cq.answer.await_args.kwargs.get("show_alert") is True


# =============================================================================
# Callback: stats / pending / health / cleanup / export (без FSM)
# =============================================================================

async def test_callback_stats_for_admin():
    cq = _make_callback(data="admin:stats")
    with patch(
        "app.bot.handlers.admin._build_stats_text",
        new=AsyncMock(return_value="📊 mocked stats"),
    ):
        await cb_admin_stats(cq)

    cq.message.answer.assert_awaited_once()
    text = cq.message.answer.await_args.args[0]
    assert "mocked stats" in text


async def test_callback_export_sends_xlsx():
    cq = _make_callback(data="admin:export")
    with patch(
        "app.bot.handlers.admin._generate_users_xlsx",
        new=AsyncMock(return_value=(b"PK\x03\x04fake-xlsx-bytes", 42)),
    ):
        await cb_admin_export(cq)

    cq.message.answer_document.assert_awaited_once()
    args, kwargs = cq.message.answer_document.await_args
    doc = args[0]
    assert doc.filename.endswith(".xlsx")
    assert doc.data[:4] == b"PK\x03\x04"
    caption = kwargs.get("caption", "")
    assert "42" in caption


async def test_callback_pending_for_admin():
    cq = _make_callback(data="admin:pending")
    with patch(
        "app.bot.handlers.admin._build_pending_text",
        new=AsyncMock(return_value="⏳ mocked pending"),
    ):
        await cb_admin_pending(cq)
    cq.message.answer.assert_awaited_once()


async def test_callback_health_for_admin():
    cq = _make_callback(data="admin:health")
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._build_health_text",
        new=AsyncMock(return_value="🏥 mocked health"),
    ) as m:
        await cb_admin_health(cq, bot=bot)
    m.assert_awaited_once_with(bot)
    cq.message.answer.assert_awaited_once()


# =============================================================================
# Close
# =============================================================================

async def test_callback_close_deletes_message():
    cq = _make_callback(data="admin:close")
    await cb_admin_close(cq)
    cq.message.delete.assert_awaited_once()
    cq.answer.assert_awaited_once()


async def test_callback_close_falls_back_to_edit_when_delete_fails():
    cq = _make_callback(data="admin:close")
    cq.message.delete = AsyncMock(side_effect=Exception("can't delete"))
    await cb_admin_close(cq)
    cq.message.edit_text.assert_awaited_once()


# =============================================================================
# FSM: find
# =============================================================================

async def test_callback_find_starts_fsm():
    cq = _make_callback(data="admin:find")
    state = _make_state()
    await cb_admin_find_start(cq, state=state)

    cq.message.answer.assert_awaited_once()
    text = cq.message.answer.await_args.args[0]
    assert "telegram_id" in text or "username" in text.lower()
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_find_query)
    cq.answer.assert_awaited_once()


async def test_fsm_find_processes_user_input():
    msg = _make_message(text="@alice")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._find_user_text",
        new=AsyncMock(return_value="👤 mocked card"),
    ) as m_find:
        await fsm_find_query(msg, state=state)

    m_find.assert_awaited_once_with("@alice")
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "mocked card" in text
    state.clear.assert_awaited_once()


async def test_fsm_find_ignores_non_admin():
    msg = _make_message(from_user_id=999, text="@alice")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._find_user_text", new=AsyncMock(),
    ) as m_find:
        await fsm_find_query(msg, state=state)
    m_find.assert_not_awaited()
    msg.answer.assert_not_awaited()


# =============================================================================
# FSM: extend
# =============================================================================

async def test_callback_extend_starts_fsm():
    cq = _make_callback(data="admin:extend")
    state = _make_state()
    await cb_admin_extend_start(cq, state=state)

    cq.message.answer.assert_awaited_once()
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_extend_input)


async def test_fsm_extend_processes_valid_input():
    msg = _make_message(text="123 7")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._extend_user",
        new=AsyncMock(return_value="✅ done"),
    ) as m:
        await fsm_extend_input(msg, state=state)
    m.assert_awaited_once_with(123, 7)
    state.clear.assert_awaited_once()


async def test_fsm_extend_validates_input_format_keeps_state():
    """Неверный формат — state НЕ сбрасывается, юзер пробует ещё."""
    msg = _make_message(text="хуйня")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._extend_user", new=AsyncMock(),
    ) as m:
        await fsm_extend_input(msg, state=state)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "формат" in text.lower() or "Неверный" in text


async def test_fsm_extend_rejects_non_numeric_keeps_state():
    msg = _make_message(text="abc def")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._extend_user", new=AsyncMock(),
    ) as m:
        await fsm_extend_input(msg, state=state)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_fsm_extend_rejects_bad_days_keeps_state():
    msg = _make_message(text="123 -5")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._extend_user", new=AsyncMock(),
    ) as m:
        await fsm_extend_input(msg, state=state)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()


# =============================================================================
# FSM: revoke (с confirmation)
# =============================================================================

async def test_callback_revoke_starts_fsm():
    cq = _make_callback(data="admin:revoke")
    state = _make_state()
    await cb_admin_revoke_start(cq, state=state)

    cq.message.answer.assert_awaited_once()
    text = cq.message.answer.await_args.args[0]
    assert "КИКНЕТ" in text or "кикн" in text.lower()  # warning
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_revoke_id)


async def test_fsm_revoke_id_shows_confirmation():
    msg = _make_message(text="123")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._find_user_text",
        new=AsyncMock(return_value="👤 user 123"),
    ):
        await fsm_revoke_id(msg, state=state)

    msg.answer.assert_awaited_once()
    args, kwargs = msg.answer.await_args
    text = args[0]
    assert "user 123" in text
    assert "отозвать" in text.lower()
    kb = kwargs.get("reply_markup")
    assert kb is not None
    cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "admin:revoke_confirm:123" in cbs
    state.clear.assert_awaited_once()


async def test_fsm_revoke_id_rejects_non_numeric():
    msg = _make_message(text="abc")
    state = _make_state()
    with patch(
        "app.bot.handlers.admin._find_user_text", new=AsyncMock(),
    ) as m:
        await fsm_revoke_id(msg, state=state)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_callback_revoke_confirm_executes_for_admin():
    cq = _make_callback(data="admin:revoke_confirm:777")
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._revoke_user",
        new=AsyncMock(return_value="✅ revoked"),
    ) as m:
        await cb_admin_revoke_confirm(cq, bot=bot)

    m.assert_awaited_once_with(bot, 777)
    cq.message.answer.assert_awaited_once()


async def test_callback_revoke_confirm_handles_bad_payload():
    """admin:revoke_confirm:not_a_number → не должен крашить, не должен звать
    _revoke_user."""
    cq = _make_callback(data="admin:revoke_confirm:nan")
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._revoke_user", new=AsyncMock(),
    ) as m:
        await cb_admin_revoke_confirm(cq, bot=bot)
    m.assert_not_awaited()


# =============================================================================
# /cancel в любом FSM-state админки
# =============================================================================

async def test_fsm_cancel_clears_state():
    msg = _make_message(text="/cancel")
    state = _make_state()
    await fsm_cancel_admin(msg, state=state)
    state.clear.assert_awaited_once()
    msg.answer.assert_awaited_once()
    text = msg.answer.await_args.args[0]
    assert "тмен" in text.lower()


# =============================================================================
# Sanity: helpers вынесены и вызываемы
# =============================================================================

async def test_helpers_extracted_and_callable():
    """Регрессия рефакторинга: имена helper'ов на месте, импортируются."""
    from app.bot.handlers.admin import (
        _build_cleanup_text,
        _build_health_text,
        _build_pending_text,
        _build_stats_text,
        _extend_user,
        _find_user_text,
        _generate_users_xlsx,
        _reduce_user,
        _revoke_user,
    )
    import inspect
    for fn in (
        _build_cleanup_text, _build_health_text, _build_pending_text,
        _build_stats_text, _extend_user, _find_user_text,
        _generate_users_xlsx, _reduce_user, _revoke_user,
    ):
        assert inspect.iscoroutinefunction(fn), f"{fn.__name__} should be async"


# =============================================================================
# admin:reduce — destructive, требует admin_only_cb защиты + FSM
# =============================================================================

async def test_callback_reduce_blocked_for_non_admin():
    """Destructive operation: не-админ не должен даже стартовать FSM."""
    cq = _make_callback(from_user_id=999, data="admin:reduce")
    state = _make_state()
    await cb_admin_reduce_start(cq, state=state)

    cq.answer.assert_awaited_once()
    assert cq.answer.await_args.kwargs.get("show_alert") is True
    state.set_state.assert_not_awaited()


async def test_callback_reduce_starts_fsm_for_admin():
    cq = _make_callback(data="admin:reduce")
    state = _make_state()
    await cb_admin_reduce_start(cq, state=state)

    cq.message.answer.assert_awaited_once()
    text = cq.message.answer.await_args.args[0]
    # Должно быть warning что юзер будет немедленно кикнут если уйдёт в expired
    assert "кикнут" in text.lower() or "кикн" in text.lower()
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_reduce_input)


async def test_fsm_reduce_processes_valid_input():
    msg = _make_message(text="123 7")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._reduce_user",
        new=AsyncMock(return_value="➖ done"),
    ) as m:
        await fsm_reduce_input(msg, state=state, bot=bot)
    m.assert_awaited_once_with(bot, 123, 7)
    state.clear.assert_awaited_once()


async def test_fsm_reduce_keeps_state_on_bad_format():
    msg = _make_message(text="мусор")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._reduce_user", new=AsyncMock(),
    ) as m:
        await fsm_reduce_input(msg, state=state, bot=bot)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()
    msg.answer.assert_awaited_once()


async def test_fsm_reduce_keeps_state_on_bad_days():
    msg = _make_message(text="123 -3")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._reduce_user", new=AsyncMock(),
    ) as m:
        await fsm_reduce_input(msg, state=state, bot=bot)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_fsm_reduce_ignores_non_admin():
    msg = _make_message(from_user_id=999, text="123 7")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._reduce_user", new=AsyncMock(),
    ) as m:
        await fsm_reduce_input(msg, state=state, bot=bot)
    m.assert_not_awaited()
    msg.answer.assert_not_awaited()


async def test_fsm_cancel_works_from_reduce_state():
    """/cancel сбрасывает state из reduce_input (через общий fsm_cancel_admin)."""
    msg = _make_message(text="/cancel")
    state = _make_state()
    await fsm_cancel_admin(msg, state=state)
    state.clear.assert_awaited_once()


# =============================================================================
# admin:import_legacy + FSM
# =============================================================================

async def test_callback_import_legacy_blocked_for_non_admin():
    cq = _make_callback(from_user_id=999, data="admin:import_legacy")
    state = _make_state()
    await cb_admin_import_legacy_start(cq, state=state)
    assert cq.answer.await_args.kwargs.get("show_alert") is True
    state.set_state.assert_not_awaited()


async def test_callback_import_legacy_starts_fsm():
    cq = _make_callback(data="admin:import_legacy")
    state = _make_state()
    await cb_admin_import_legacy_start(cq, state=state)
    cq.message.answer.assert_awaited_once()
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_import_legacy_input)


async def test_fsm_import_legacy_processes_valid_input():
    msg = _make_message(text="123 30")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._import_legacy_one_text",
        new=AsyncMock(return_value="✅ done"),
    ) as m:
        await fsm_import_legacy_input(msg, state=state, bot=bot)
    m.assert_awaited_once_with(123, 30)
    state.clear.assert_awaited_once()


async def test_fsm_import_legacy_keeps_state_on_bad_format():
    msg = _make_message(text="мусор")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._import_legacy_one_text", new=AsyncMock(),
    ) as m:
        await fsm_import_legacy_input(msg, state=state, bot=bot)
    m.assert_not_awaited()
    state.clear.assert_not_awaited()


# =============================================================================
# admin:grant + FSM
# =============================================================================

async def test_callback_grant_blocked_for_non_admin():
    cq = _make_callback(from_user_id=999, data="admin:grant")
    state = _make_state()
    await cb_admin_grant_start(cq, state=state)
    assert cq.answer.await_args.kwargs.get("show_alert") is True
    state.set_state.assert_not_awaited()


async def test_callback_grant_starts_fsm():
    cq = _make_callback(data="admin:grant")
    state = _make_state()
    await cb_admin_grant_start(cq, state=state)
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_grant_input)


async def test_fsm_grant_input_calls_grant_user():
    msg = _make_message(text="123 7")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._grant_user",
        new=AsyncMock(return_value="🎁 ok"),
    ) as m:
        await fsm_grant_input(msg, state=state, bot=bot)
    m.assert_awaited_once_with(bot, 123, 7)
    state.clear.assert_awaited_once()


# =============================================================================
# admin:reissue + FSM
# =============================================================================

async def test_callback_reissue_blocked_for_non_admin():
    cq = _make_callback(from_user_id=999, data="admin:reissue")
    state = _make_state()
    await cb_admin_reissue_start(cq, state=state)
    assert cq.answer.await_args.kwargs.get("show_alert") is True


async def test_callback_reissue_starts_fsm():
    cq = _make_callback(data="admin:reissue")
    state = _make_state()
    await cb_admin_reissue_start(cq, state=state)
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_reissue_input)


async def test_fsm_reissue_input_calls_reissue_user():
    msg = _make_message(text="555")
    state = _make_state()
    bot = MagicMock()
    with patch(
        "app.bot.handlers.admin._reissue_user",
        new=AsyncMock(return_value="🔁 done"),
    ) as m:
        await fsm_reissue_input(msg, state=state, bot=bot)
    m.assert_awaited_once_with(bot, 555)
    state.clear.assert_awaited_once()


# =============================================================================
# admin:bulk_import + FSM (parsing, preview, confirm)
# =============================================================================

async def test_callback_bulk_import_blocked_for_non_admin():
    cq = _make_callback(from_user_id=999, data="admin:bulk_import")
    state = _make_state()
    await cb_admin_bulk_import_start(cq, state=state)
    assert cq.answer.await_args.kwargs.get("show_alert") is True


async def test_callback_bulk_import_starts_fsm():
    cq = _make_callback(data="admin:bulk_import")
    state = _make_state()
    await cb_admin_bulk_import_start(cq, state=state)
    state.set_state.assert_awaited_once_with(AdminFlow.waiting_bulk_import_input)


async def test_fsm_bulk_import_parses_mixed_lines(monkeypatch):
    """Парсинг + резолв: 2 валидных id, 1 валидный @username, 1 invalid days,
    1 пустая, 1 комментарий."""
    msg = _make_message(text=(
        "123 30\n"
        "@petrov 15\n"
        "# комментарий\n"
        "\n"
        "456 0\n"          # bad: days <= 0
        "789 7\n"
    ))
    state = _make_state()

    bot = MagicMock()
    chat = MagicMock(); chat.id = 999
    bot.get_chat = AsyncMock(return_value=chat)

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])  # все юзеры новые
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    await fsm_bulk_import_input(msg, state=state, bot=bot)

    # Бот ответил preview-сообщением.
    msg.answer.assert_awaited()
    text = msg.answer.await_args.args[0]
    assert "Будет импортировано: 3" in text
    assert "Ошибок: 1" in text
    # state переключился на confirm
    state.set_state.assert_awaited_with(AdminFlow.waiting_bulk_import_confirm)


async def test_fsm_bulk_import_too_many(monkeypatch):
    """Лимит 100: 101 строка → отказ."""
    lines = [f"{i} 30" for i in range(101)]
    msg = _make_message(text="\n".join(lines))
    state = _make_state()
    bot = MagicMock()
    pool = MagicMock()
    monkeypatch.setattr("app.bot.handlers.admin.get_pool", lambda: pool)

    await fsm_bulk_import_input(msg, state=state, bot=bot)

    text = msg.answer.await_args.args[0]
    assert "Слишком много" in text
    state.clear.assert_awaited_once()


async def test_fsm_bulk_import_nothing_to_import(monkeypatch):
    """Только комментарии — отказ."""
    msg = _make_message(text="# foo\n# bar\n\n")
    state = _make_state()
    bot = MagicMock()
    await fsm_bulk_import_input(msg, state=state, bot=bot)
    text = msg.answer.await_args.args[0]
    assert "Нет валидных строк" in text
    state.clear.assert_awaited_once()


async def test_callback_bulk_import_confirm_executes_imports(monkeypatch):
    """Confirm: для каждого resolved entry вызывается import_legacy_user."""
    cq = _make_callback(data="admin:bulk_import_confirm")
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={
        "resolved": [(100, 30), (200, 7), (300, 5)],
        "errors": [],
    })
    state.clear = AsyncMock()

    fake_until = datetime(2026, 6, 1, tzinfo=timezone.utc)
    from app.db.queries import ImportLegacyResult
    m_import = AsyncMock(side_effect=[
        ImportLegacyResult(new_paid_until=fake_until, was_created=True, granted_count=4),
        ImportLegacyResult(new_paid_until=fake_until, was_created=False, granted_count=4),
        ImportLegacyResult(new_paid_until=fake_until, was_created=True, granted_count=4),
    ])
    monkeypatch.setattr("app.bot.handlers.admin.import_legacy_user", m_import)
    monkeypatch.setattr("app.bot.handlers.admin.settings.chat_ids", [-1, -2, -3, -4])

    await cb_admin_bulk_import_confirm(cq, state=state)

    assert m_import.await_count == 3
    cq.message.answer.assert_awaited()
    text = cq.message.answer.await_args.args[0]
    assert "Импорт завершён" in text
    assert "OK: 3" in text
    assert "создано новых: 2" in text
    assert "продлено существующих: 1" in text
    assert "Granted_access: 12" in text  # 3 × 4
    state.clear.assert_awaited_once()


async def test_callback_bulk_import_confirm_blocked_for_non_admin():
    cq = _make_callback(from_user_id=999, data="admin:bulk_import_confirm")
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"resolved": [(100, 30)], "errors": []})
    m_import = AsyncMock()
    with patch("app.bot.handlers.admin.import_legacy_user", new=m_import):
        await cb_admin_bulk_import_confirm(cq, state=state)
    m_import.assert_not_awaited()
