"""Админ-функционал: 8 команд через слэш + inline-панель /admin.

Архитектура:
  * helper-функции (`_find_user_text`, `_build_stats_text`, `_extend_user`,
    `_revoke_user`, и т.д.) содержат всю бизнес-логику и возвращают готовый
    текст или артефакт. Они переиспользуются и в slash-командах, и в
    callback-хендлерах панели.
  * `admin_only` — декоратор для message-handlers (silent no-op для не-админов).
  * `admin_only_cb` — декоратор для callback-handlers (alert "Нет доступа",
    проверка ОБЯЗАТЕЛЬНА на каждом callback — иначе любой может прислать
    `admin:revoke_confirm:<id>` и кикнуть).
  * `AdminFlow` — FSM для команд с аргументами (find, extend, revoke).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from typing import Awaitable, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot.keyboards import admin_panel_kb, bulk_import_confirm_kb, revoke_confirm_kb
from app.bot.texts import (
    ADMIN_NO_ACCESS,
    ADMIN_PANEL_TEXT,
    BULK_IMPORT_DONE,
    BULK_IMPORT_NOTHING_TO_IMPORT,
    BULK_IMPORT_PREVIEW,
    BULK_IMPORT_PROMPT,
    BULK_IMPORT_RESOLVING,
    BULK_IMPORT_TOO_MANY,
    GRANT_FORBIDDEN_FALLBACK,
    GRANT_NO_LINKS,
    GRANT_OK,
    GRANT_PROMPT,
    GRANT_USAGE,
    IMPORT_LEGACY_OK,
    IMPORT_LEGACY_PROMPT,
    IMPORT_LEGACY_RESOLVE_FAIL,
    IMPORT_LEGACY_USAGE,
    LEGACY_USER_FLAG,
    PAID_WITH_LINKS,
    REDUCE_PROMPT,
    REDUCE_USAGE,
    REDUCE_USER_NOT_FOUND,
    REISSUE_EXPIRED,
    REISSUE_FORBIDDEN_FALLBACK,
    REISSUE_NO_LINKS,
    REISSUE_OK,
    REISSUE_PROMPT,
    REISSUE_USAGE,
)
from app.config import settings
from app.db.pool import get_pool
from app.db.queries import (
    count_active_breakdown,
    count_legacy_active_users,
    count_user_access_breakdown,
    get_user_paid_until,
    grant_access,
    import_legacy_user,
    is_legacy_active_user,
    mark_revoked_by_link,
    process_paid_invoice,  # noqa: F401  # used elsewhere; kept import for clarity
    reduce_subscription,
    revoke_all_active_for_user,
)

log = logging.getLogger(__name__)
router = Router()


# =============================================================================
# Access control
# =============================================================================

def admin_only(handler: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
    """Silent no-op для не-админов (для message-handlers)."""
    @wraps(handler)
    async def wrapper(message: Message, *args, **kwargs):
        if (
            settings.admin_telegram_id == 0
            or message.from_user is None
            or message.from_user.id != settings.admin_telegram_id
        ):
            return
        return await handler(message, *args, **kwargs)
    return wrapper


def admin_only_cb(handler: Callable[..., Awaitable]) -> Callable[..., Awaitable]:
    """Alert + return для не-админов (для callback-handlers).
    КРИТИЧНО: без этого декоратора любой может прислать admin:* callback.
    """
    @wraps(handler)
    async def wrapper(cq: CallbackQuery, *args, **kwargs):
        if (
            settings.admin_telegram_id == 0
            or cq.from_user is None
            or cq.from_user.id != settings.admin_telegram_id
        ):
            try:
                await cq.answer(ADMIN_NO_ACCESS, show_alert=True)
            except Exception:
                pass
            return
        return await handler(cq, *args, **kwargs)
    return wrapper


# =============================================================================
# FSM
# =============================================================================

class AdminFlow(StatesGroup):
    waiting_find_query = State()
    waiting_extend_input = State()             # "<id> <days>"
    waiting_reduce_input = State()             # "<id> <days>"
    waiting_revoke_id = State()
    waiting_import_legacy_input = State()      # "<id|@username> <days>"
    waiting_bulk_import_input = State()        # многострочный
    waiting_bulk_import_confirm = State()      # ждём кнопку Запустить
    waiting_grant_input = State()              # "<id|@username> <days>"
    waiting_reissue_input = State()            # "<id|@username>"


# =============================================================================
# Helpers
# =============================================================================

def _fmt_dt(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M UTC") -> str:
    if dt is None:
        return "—"
    return dt.strftime(fmt)


async def _find_user_text(query: str) -> str:
    """По строке-запросу (telegram_id или @username) возвращает готовый
    HTML-текст карточки юзера, либо текст ошибки/'не найден'."""
    pool = get_pool()
    if query.startswith("@"):
        username = query[1:]
        user = await pool.fetchrow(
            "SELECT * FROM users WHERE LOWER(username) = LOWER($1)", username,
        )
    else:
        try:
            tg_id = int(query)
        except ValueError:
            return "Bad input. Use telegram_id (number) or @username."
        user = await pool.fetchrow("SELECT * FROM users WHERE telegram_id = $1", tg_id)

    if not user:
        return f"❌ Юзер не найден: {query}"

    legacy = await is_legacy_active_user(user["telegram_id"])
    breakdown = await count_user_access_breakdown(user["telegram_id"])

    payments = await pool.fetch(
        """SELECT plan, amount, status, provider, created_at
             FROM payments WHERE telegram_id = $1
            ORDER BY created_at DESC LIMIT 5""",
        user["telegram_id"],
    )
    granted = await pool.fetch(
        """SELECT chat_id, paid_until, joined_at, revoked_at
             FROM granted_access WHERE telegram_id = $1
            ORDER BY id DESC""",
        user["telegram_id"],
    )

    lines = [
        f"👤 <b>Юзер {user['telegram_id']}</b>",
        f"Username: @{user['username'] or '—'}",
        f"Тариф: {user['plan'] or '—'}",
        f"Подписка до: {_fmt_dt(user['paid_until'])}",
        f"Статус: {user['status']}",
        f"Регистрация: {_fmt_dt(user['created_at'], '%d.%m.%Y')}",
    ]
    if legacy:
        lines.append(LEGACY_USER_FLAG)
    lines.extend([
        "",
        f"🔗 Доступ: {breakdown['via_invite']} через бот-ссылки, "
        f"{breakdown['via_legacy']} через legacy-импорт",
        "",
        "💳 <b>Последние 5 платежей:</b>",
    ])
    if payments:
        for p in payments:
            lines.append(
                f"  {_fmt_dt(p['created_at'], '%d.%m %H:%M')} • {p['plan']} • "
                f"{p['amount']}$ • {p['status']} • {p['provider']}"
            )
    else:
        lines.append("  нет платежей")

    lines.extend(["", "🔗 <b>Доступы (granted_access):</b>"])
    if granted:
        for g in granted:
            joined = "✅ вступил" if g["joined_at"] else "⏳ не вступал"
            revoked = " | 🚫 revoked" if g["revoked_at"] else ""
            lines.append(
                f"  chat={g['chat_id']} • до "
                f"{_fmt_dt(g['paid_until'], '%d.%m.%Y')} • {joined}{revoked}"
            )
    else:
        lines.append("  нет записей")

    return "\n".join(lines)


async def _generate_users_xlsx() -> tuple[bytes, int]:
    """Возвращает (xlsx-bytes, count). openpyxl импортируется лениво."""
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT
            u.telegram_id,
            u.username,
            u.plan,
            u.paid_until,
            u.status,
            u.created_at,
            COALESCE(SUM(p.amount) FILTER (WHERE p.status='paid'), 0) AS total_paid,
            COUNT(p.id) FILTER (WHERE p.status='paid') AS payment_count,
            MAX(p.created_at) FILTER (WHERE p.status='paid') AS last_payment,
            (SELECT provider FROM payments
              WHERE telegram_id = u.telegram_id AND status='paid'
              ORDER BY created_at DESC LIMIT 1) AS last_provider,
            (SELECT COUNT(*) FROM granted_access
              WHERE telegram_id = u.telegram_id
                AND joined_at IS NOT NULL
                AND revoked_at IS NULL) AS active_chats
          FROM users u
          LEFT JOIN payments p ON p.telegram_id = u.telegram_id
         GROUP BY u.telegram_id
         ORDER BY u.paid_until DESC NULLS LAST
        """
    )

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Users"

    headers = [
        "Telegram ID", "Username", "Plan", "Paid Until (UTC)", "Status",
        "Created", "Total Paid USDT", "Payment Count", "Last Payment",
        "Last Provider", "Active Chats",
    ]
    ws.append(headers)

    for r in rows:
        ws.append([
            r["telegram_id"],
            r["username"] or "",
            r["plan"] or "",
            _fmt_dt(r["paid_until"], "%d.%m.%Y %H:%M"),
            r["status"],
            _fmt_dt(r["created_at"], "%d.%m.%Y"),
            float(r["total_paid"]),
            int(r["payment_count"]),
            _fmt_dt(r["last_payment"], "%d.%m.%Y %H:%M"),
            r["last_provider"] or "",
            int(r["active_chats"]),
        ])

    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 30)

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue(), len(rows)


async def _build_stats_text() -> str:
    pool = get_pool()
    user_stats = await pool.fetchrow(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status='active' AND paid_until > NOW()) AS active,
            COUNT(*) FILTER (WHERE status='expired' OR paid_until <= NOW()) AS expired,
            COUNT(*) FILTER (WHERE status='new' AND paid_until IS NULL) AS new_users
          FROM users
        """
    )
    plan_stats = await pool.fetch(
        """
        SELECT plan, COUNT(*) AS cnt
          FROM users
         WHERE status='active' AND paid_until > NOW()
         GROUP BY plan
         ORDER BY cnt DESC
        """
    )
    revenue = await pool.fetchrow(
        """
        SELECT
            COALESCE(SUM(amount) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours'), 0) AS day,
            COALESCE(SUM(amount) FILTER (WHERE created_at > NOW() - INTERVAL '7 days'), 0) AS week,
            COALESCE(SUM(amount) FILTER (WHERE created_at > NOW() - INTERVAL '30 days'), 0) AS month,
            COALESCE(SUM(amount), 0) AS total
          FROM payments
         WHERE status='paid'
        """
    )
    by_provider = await pool.fetch(
        """
        SELECT provider, COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS revenue
          FROM payments
         WHERE status='paid'
         GROUP BY provider
         ORDER BY revenue DESC
        """
    )

    lines = [
        "📊 <b>Статистика</b>",
        "",
        "<b>Юзеры:</b>",
        f"  Всего: {user_stats['total']}",
        f"  Активных: {user_stats['active']}",
        f"  Просрочено: {user_stats['expired']}",
        f"  Новые (без подписки): {user_stats['new_users']}",
        "",
        "<b>Активные подписки по тарифам:</b>",
    ]
    if plan_stats:
        for p in plan_stats:
            lines.append(f"  {p['plan']}: {p['cnt']}")
    else:
        lines.append("  нет активных")

    lines.extend([
        "",
        "<b>Оборот (USDT):</b>",
        f"  За сутки: {float(revenue['day']):.2f}",
        f"  За неделю: {float(revenue['week']):.2f}",
        f"  За месяц: {float(revenue['month']):.2f}",
        f"  Всего: {float(revenue['total']):.2f}",
        "",
        "<b>По провайдерам:</b>",
    ])
    if by_provider:
        for p in by_provider:
            lines.append(
                f"  {p['provider']}: {p['cnt']} платежей • {float(p['revenue']):.2f} USDT"
            )
    else:
        lines.append("  ещё нет paid платежей")

    return "\n".join(lines)


async def _extend_user(tg_id: int, days: int) -> str:
    """Та же формула что в process_paid_invoice. Создаёт юзера если его нет.
    Параллельно растягивает paid_until в активных granted_access."""
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO users (telegram_id, status)
        VALUES ($1, 'new')
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        tg_id,
    )
    new_paid_until = await pool.fetchval(
        """
        UPDATE users
           SET paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW())
                          + make_interval(days => $2),
               status = 'active'
         WHERE telegram_id = $1
        RETURNING paid_until
        """,
        tg_id, days,
    )
    await pool.execute(
        """
        UPDATE granted_access
           SET paid_until = $2
         WHERE telegram_id = $1
           AND joined_at IS NOT NULL
           AND revoked_at IS NULL
        """,
        tg_id, new_paid_until,
    )
    log.info("Admin extended tg=%s by %s days, new paid_until=%s", tg_id, days, new_paid_until)
    return (
        f"✅ Юзеру <code>{tg_id}</code> добавлено <b>{days} дн.</b>\n"
        f"Новый срок: {_fmt_dt(new_paid_until)}"
    )


async def _resolve_target(bot: Bot, ident: str) -> tuple[int | None, str | None]:
    """Резолвит идентификатор юзера в telegram_id.
      * Числовая строка → int.
      * '@username' → bot.get_chat(...).id.
    Возвращает (tg_id, error). На ошибку tg_id=None, error содержит описание."""
    ident = ident.strip()
    if not ident:
        return None, "пустой идентификатор"
    if ident.startswith("@"):
        username = ident[1:]
        try:
            chat = await bot.get_chat(ident)
        except Exception as e:
            return None, f"не удалось резолвить @{username}: {e}"
        return int(chat.id), None
    try:
        return int(ident), None
    except ValueError:
        return None, "telegram_id должен быть числом или @username"


async def _grant_access_to_user(
    bot: Bot, telegram_id: int, paid_until: datetime,
) -> tuple[list[str], bool]:
    """Создаёт invite-ссылки во всех CHAT_IDS, регистрирует в granted_access,
    пытается отправить юзеру в ЛС. Возвращает (links, sent_to_user).
    sent_to_user=False означает TelegramForbiddenError или другую ошибку
    отправки — админу нужно переслать вручную.

    Дублирует часть логики из chats.manager.issue_invite_links_and_send,
    но возвращает данные для админ-ответа (а не глотает ошибки)."""
    expire_at = datetime.now(timezone.utc) + timedelta(hours=1)
    links: list[str] = []
    for chat_id in settings.chat_ids:
        try:
            link = await bot.create_chat_invite_link(
                chat_id=chat_id,
                expire_date=expire_at,
                creates_join_request=True,
                name=f"sub_{telegram_id}_{int(expire_at.timestamp())}",
            )
        except Exception as e:
            log.error("admin grant: create_chat_invite_link chat=%s failed: %s", chat_id, e)
            continue
        try:
            await grant_access(telegram_id, chat_id, link.invite_link, paid_until)
        except Exception as e:
            log.error("admin grant: grant_access tg=%s chat=%s failed: %s", telegram_id, chat_id, e)
            continue
        links.append(link.invite_link)

    if not links:
        return links, False

    text = PAID_WITH_LINKS.format(
        links="\n".join(f"{i+1}. {url}" for i, url in enumerate(links))
    )
    try:
        await bot.send_message(telegram_id, text, disable_web_page_preview=True)
        return links, True
    except Exception as e:
        log.warning("admin grant: send_message tg=%s failed: %s", telegram_id, e)
        return links, False


def _format_links_for_admin(links: list[str]) -> str:
    return "\n".join(f"{i+1}. {url}" for i, url in enumerate(links))


async def _reduce_user(bot: Bot, tg_id: int, days: int) -> str:
    """Зеркало _extend_user: уменьшает paid_until. Если уходит в прошлое —
    немедленно кикает по active granted_access (без ожидания крон-тика).
    Возвращает HTML-текст итога."""
    result = await reduce_subscription(tg_id, days)
    if not result.found:
        return REDUCE_USER_NOT_FOUND.format(tg_id=tg_id)

    log.info(
        "Admin reduced tg=%s by %s days, new paid_until=%s, expired=%s",
        tg_id, days, result.new_paid_until, result.now_expired,
    )

    kicked = 0
    failed = 0
    if result.now_expired:
        for chat_id, invite_link in result.active_granted:
            try:
                await bot.ban_chat_member(chat_id=chat_id, user_id=tg_id)
                await asyncio.sleep(0.05)
                await bot.unban_chat_member(
                    chat_id=chat_id, user_id=tg_id, only_if_banned=True,
                )
            except TelegramBadRequest as e:
                # Юзера в чате нет / уже забанен — нормально, считаем revoked.
                log.info(
                    "reduce-kick tg=%s chat=%s bad_request (treated as revoked): %s",
                    tg_id, chat_id, e,
                )
            except TelegramRetryAfter as e:
                # Не кикаем сейчас — крон попробует позже.
                log.warning(
                    "reduce-kick tg=%s chat=%s rate-limited, deferring to cron: retry_after=%s",
                    tg_id, chat_id, e.retry_after,
                )
                continue
            except Exception as e:
                log.error("reduce-kick tg=%s chat=%s failed: %s", tg_id, chat_id, e)
                continue

            try:
                await mark_revoked_by_link(invite_link)
                kicked += 1
            except Exception as e:
                log.error("mark_revoked link=%s failed: %s", invite_link, e)
                failed += 1

    status_word = "🚫 expired" if result.now_expired else "✅ active"
    lines = [
        f"➖ Юзеру <code>{tg_id}</code> срок уменьшен на <b>{days} дн.</b>",
        f"Новый срок: {_fmt_dt(result.new_paid_until)}",
        f"Статус: {status_word}",
    ]
    if result.now_expired:
        lines.append(f"Кикнут из {kicked} каналов.")
        if failed:
            lines.append(f"Ошибок при revoke: {failed}")
    return "\n".join(lines)


async def _revoke_user(bot: Bot, tg_id: int) -> str:
    """Кикает по всем активным granted_access + помечает users.expired.
    Возвращает HTML-текст с итогами."""
    pool = get_pool()
    granted = await pool.fetch(
        """SELECT id, chat_id FROM granted_access
            WHERE telegram_id = $1 AND revoked_at IS NULL""",
        tg_id,
    )

    kicked = 0
    failed = 0
    for g in granted:
        try:
            await bot.ban_chat_member(chat_id=g["chat_id"], user_id=tg_id)
            await asyncio.sleep(0.05)
            await bot.unban_chat_member(chat_id=g["chat_id"], user_id=tg_id)
            await pool.execute(
                "UPDATE granted_access SET revoked_at = NOW() WHERE id = $1", g["id"],
            )
            kicked += 1
        except Exception as e:
            log.warning("revoke kick failed tg=%s chat=%s: %s", tg_id, g["chat_id"], e)
            failed += 1

    await pool.execute(
        "UPDATE users SET status='expired', paid_until=NOW() WHERE telegram_id = $1",
        tg_id,
    )
    log.info("Admin revoked tg=%s: kicked=%s failed=%s", tg_id, kicked, failed)
    return (
        f"✅ Юзер <code>{tg_id}</code> отозван.\n"
        f"Кикнут из {kicked} каналов.\n"
        f"Ошибок: {failed}"
    )


async def _build_pending_text() -> str:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT id, telegram_id, plan, amount, provider, created_at,
               EXTRACT(EPOCH FROM (NOW() - created_at)) / 60 AS age_minutes
          FROM payments
         WHERE status = 'pending'
           AND created_at < NOW() - INTERVAL '1 hour'
         ORDER BY created_at DESC
         LIMIT 30
        """
    )
    if not rows:
        return "✅ Нет зависших pending платежей старше 1 часа."

    lines = [f"⏳ <b>{len(rows)} pending платежей старше 1 часа:</b>", ""]
    for r in rows:
        age_h = int(r["age_minutes"] / 60)
        lines.append(
            f"#{r['id']} • tg={r['telegram_id']} • {r['plan']} • "
            f"{r['amount']}$ • {r['provider']} • {age_h}ч назад"
        )
    if len(rows) >= 30:
        lines.append("")
        lines.append("(показаны первые 30, всего может быть больше)")
    return "\n".join(lines)


async def _build_health_text(bot: Bot) -> str:
    pool = get_pool()
    lines = ["🏥 <b>Health check</b>", ""]

    try:
        await pool.fetchval("SELECT 1")
        lines.append("✅ Database: ok")
    except Exception as e:
        lines.append(f"❌ Database: {e}")

    lines.append(f"✅ Providers active: {', '.join(settings.enabled_providers)}")

    last_paid = await pool.fetchrow(
        "SELECT created_at, provider FROM payments WHERE status='paid' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if last_paid:
        ago_min = int(
            (datetime.now(timezone.utc) - last_paid["created_at"]).total_seconds() / 60
        )
        lines.append(f"💳 Последний paid: {ago_min} мин назад ({last_paid['provider']})")
    else:
        lines.append("💳 Платежей ещё нет")

    pending_count = await pool.fetchval(
        "SELECT COUNT(*) FROM payments "
        "WHERE status='pending' AND created_at < NOW() - INTERVAL '1 hour'"
    )
    if pending_count and pending_count > 0:
        lines.append(f"⚠️ Зависших pending: {pending_count} (см. /pending)")
    else:
        lines.append("✅ Pending зависших: 0")

    active_granted = await pool.fetchval(
        "SELECT COUNT(*) FROM granted_access "
        "WHERE joined_at IS NOT NULL AND revoked_at IS NULL"
    )
    lines.append(f"🔗 Активных granted_access: {active_granted}")

    # Разбивка active-юзеров по типу присутствия в чатах.
    bd = await count_active_breakdown()
    lines.extend([
        "",
        f"<b>Состав активных:</b>",
        f"  Active в users: {bd['active_users']}",
        f"  ├─ Через бот-ссылки (joined_at): {bd['via_invite']}",
        f"  ├─ Импортированы как legacy: {bd['via_legacy']}",
        f"  └─ Legacy без импорта: {bd['legacy_no_import']}",
    ])
    if bd["legacy_no_import"] > 0:
        lines.append(
            f"⚠️ {bd['legacy_no_import']} юзеров не будут кикнуты "
            f"(нужен /import_legacy или /bulk_import_legacy)."
        )

    # Старая метрика (legacy без активных granted-записей) — оставлена для
    # сравнения с count_active_breakdown.legacy_no_import (должны совпадать).
    legacy_count = await count_legacy_active_users()
    if legacy_count != bd["legacy_no_import"]:
        # Сигнализируем о расхождении (диагностика на случай гонок данных).
        lines.append(
            f"  (diagnostic: count_legacy_active_users={legacy_count})"
        )

    chats_ok = 0
    chats_err = 0
    for chat_id in settings.chat_ids:
        try:
            await bot.get_chat_member_count(chat_id)
            chats_ok += 1
        except Exception:
            chats_err += 1
    if chats_err == 0:
        lines.append(f"✅ Все {chats_ok} каналов доступны")
    else:
        lines.append(f"⚠️ Каналы: {chats_ok} ok, {chats_err} с ошибками")

    return "\n".join(lines)


async def _build_cleanup_text(bot: Bot) -> str:
    pool = get_pool()
    lines = [
        "🧹 <b>Cleanup chats</b>",
        "",
        "⚠️ Telegram Bot API не позволяет получить полный список участников канала.",
        "Поэтому массовая чистка автоматически невозможна.",
        "",
    ]
    for chat_id in settings.chat_ids:
        try:
            count = await bot.get_chat_member_count(chat_id)
            chat = await bot.get_chat(chat_id)
            title = getattr(chat, "title", "—") or "—"
            lines.append(f"<b>{title}</b> ({chat_id})")
            lines.append(f"  Участников: {count}")
        except Exception as e:
            lines.append(f"chat={chat_id}: ошибка {e}")

    active_granted = await pool.fetchval(
        """SELECT COUNT(*) FROM granted_access
           WHERE joined_at IS NOT NULL AND revoked_at IS NULL"""
    )
    active_users = await pool.fetchval(
        """SELECT COUNT(*) FROM users
           WHERE status='active' AND paid_until > NOW()"""
    )
    lines.extend([
        "",
        "<b>В БД:</b>",
        f"  Активных granted_access: {active_granted}",
        f"  Активных подписок (users): {active_users}",
        "",
        "Если в каналах больше участников чем активных granted_access — это",
        "халявщики из до-фикса (member_limit=1, до перехода на",
        "creates_join_request=True). Чистка — Telethon-скрипт или вручную.",
    ])
    return "\n".join(lines)


# =============================================================================
# Slash-commands (старый интерфейс — оставлен полностью совместимым)
# =============================================================================

@router.message(Command("admin"))
@admin_only
async def cmd_admin(message: Message) -> None:
    """Главная админ-панель — inline-клавиатура."""
    await message.answer(ADMIN_PANEL_TEXT, reply_markup=admin_panel_kb(), parse_mode="HTML")


@router.message(Command("find"))
@admin_only
async def cmd_find(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /find <telegram_id> or /find @username")
        return
    text = await _find_user_text(command.args.strip())
    await message.answer(text, parse_mode="HTML")


@router.message(Command("export"))
@admin_only
async def cmd_export(message: Message) -> None:
    data, count = await _generate_users_xlsx()
    filename = f"users_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.xlsx"
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"📊 Выгрузка: {count} юзеров",
    )


@router.message(Command("stats"))
@admin_only
async def cmd_stats(message: Message) -> None:
    text = await _build_stats_text()
    await message.answer(text, parse_mode="HTML")


@router.message(Command("extend"))
@admin_only
async def cmd_extend(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /extend <telegram_id> <days>")
        return
    parts = command.args.split()
    if len(parts) != 2:
        await message.answer("Usage: /extend <telegram_id> <days>")
        return
    try:
        tg_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        await message.answer("Bad input. Both telegram_id and days must be numbers.")
        return
    if days <= 0 or days > 365:
        await message.answer("Days must be between 1 and 365.")
        return
    text = await _extend_user(tg_id, days)
    await message.answer(text, parse_mode="HTML")


def _parse_id_days(args: str | None) -> tuple[int, int] | str:
    """Общий парсер для extend / reduce. Возвращает (tg_id, days) или
    строку-ошибку для отправки юзеру."""
    if not args:
        return "Usage: <telegram_id> <days>"
    parts = args.split()
    if len(parts) != 2:
        return "Usage: <telegram_id> <days>"
    try:
        tg_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        return "Bad input. Both telegram_id and days must be numbers."
    if days <= 0 or days > 365:
        return "Days must be between 1 and 365."
    return tg_id, days


@router.message(Command("reduce"))
@admin_only
async def cmd_reduce(message: Message, command: CommandObject, bot: Bot) -> None:
    parsed = _parse_id_days(command.args)
    if isinstance(parsed, str):
        # Обёртка вокруг общего usage-сообщения с правильной командой
        if parsed.startswith("Usage:"):
            await message.answer(REDUCE_USAGE)
        else:
            await message.answer(parsed)
        return
    tg_id, days = parsed
    text = await _reduce_user(bot, tg_id, days)
    await message.answer(text, parse_mode="HTML")


@router.message(Command("revoke"))
@admin_only
async def cmd_revoke(message: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args:
        await message.answer("Usage: /revoke <telegram_id>")
        return
    try:
        tg_id = int(command.args.strip())
    except ValueError:
        await message.answer("Bad input. telegram_id must be a number.")
        return
    text = await _revoke_user(bot, tg_id)
    await message.answer(text, parse_mode="HTML")


# --- /import_legacy ---------------------------------------------------------

@router.message(Command("import_legacy"))
@admin_only
async def cmd_import_legacy(message: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args:
        await message.answer(IMPORT_LEGACY_USAGE)
        return
    parts = command.args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(IMPORT_LEGACY_USAGE)
        return
    ident, days_str = parts[0], parts[1].strip()
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("Days must be a number.")
        return
    if days <= 0 or days > 365:
        await message.answer("Days must be between 1 and 365.")
        return

    tg_id, err = await _resolve_target(bot, ident)
    if tg_id is None:
        await message.answer(f"❌ {err}")
        return

    text = await _import_legacy_one_text(tg_id, days)
    await message.answer(text, parse_mode="HTML")


async def _import_legacy_one_text(tg_id: int, days: int) -> str:
    result = await import_legacy_user(tg_id, days, settings.chat_ids)
    log.info(
        "Admin imported legacy tg=%s days=%s created=%s granted=%s",
        tg_id, days, result.was_created, result.granted_count,
    )
    return IMPORT_LEGACY_OK.format(
        tg_id=tg_id,
        paid_until_str=_fmt_dt(result.new_paid_until),
        granted=result.granted_count,
        created="да" if result.was_created else "нет",
    )


# --- /grant -----------------------------------------------------------------

@router.message(Command("grant"))
@admin_only
async def cmd_grant(message: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args:
        await message.answer(GRANT_USAGE)
        return
    parts = command.args.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(GRANT_USAGE)
        return
    ident, days_str = parts[0], parts[1].strip()
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("Days must be a number.")
        return
    if days <= 0 or days > 365:
        await message.answer("Days must be between 1 and 365.")
        return

    tg_id, err = await _resolve_target(bot, ident)
    if tg_id is None:
        await message.answer(f"❌ {err}")
        return

    text = await _grant_user(bot, tg_id, days)
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


async def _grant_user(bot: Bot, tg_id: int, days: int) -> str:
    """Выдача доступа: extend подписки + issue invite-ссылок + ответ админу
    (с фоллбэком на пересылку, если юзер не доступен)."""
    extend_text = await _extend_user(tg_id, days)  # noqa: F841 — лог уже внутри
    paid_until = await get_user_paid_until(tg_id)
    if paid_until is None:
        return f"⚠️ Не удалось получить paid_until для tg=<code>{tg_id}</code>."

    links, sent = await _grant_access_to_user(bot, tg_id, paid_until)
    if not links:
        return GRANT_NO_LINKS.format(tg_id=tg_id)
    if sent:
        return GRANT_OK.format(
            tg_id=tg_id, days=days, paid_until_str=_fmt_dt(paid_until),
        )
    return GRANT_FORBIDDEN_FALLBACK.format(
        tg_id=tg_id, days=days, paid_until_str=_fmt_dt(paid_until),
        links=_format_links_for_admin(links),
    )


# --- /reissue ---------------------------------------------------------------

@router.message(Command("reissue"))
@admin_only
async def cmd_reissue(message: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args:
        await message.answer(REISSUE_USAGE)
        return
    ident = command.args.strip().split(maxsplit=1)[0]
    tg_id, err = await _resolve_target(bot, ident)
    if tg_id is None:
        await message.answer(f"❌ {err}")
        return

    text = await _reissue_user(bot, tg_id)
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


async def _reissue_user(bot: Bot, tg_id: int) -> str:
    paid_until = await get_user_paid_until(tg_id)
    now = datetime.now(timezone.utc)
    if paid_until is None or paid_until <= now:
        return REISSUE_EXPIRED.format(tg_id=tg_id)

    revoked = await revoke_all_active_for_user(tg_id)
    log.info("Admin reissue tg=%s: revoked %s old granted_access", tg_id, revoked)

    links, sent = await _grant_access_to_user(bot, tg_id, paid_until)
    if not links:
        return REISSUE_NO_LINKS.format(tg_id=tg_id, revoked=revoked)
    if sent:
        return REISSUE_OK.format(n=len(links), tg_id=tg_id)
    return REISSUE_FORBIDDEN_FALLBACK.format(
        n=len(links), tg_id=tg_id,
        links=_format_links_for_admin(links),
    )


# --- /bulk_import_legacy ----------------------------------------------------

BULK_IMPORT_MAX_LINES = 100


def _parse_bulk_import_lines(text: str) -> tuple[list[tuple[str, int]], list[str]]:
    """Возвращает (entries, errors). entries: список (ident, days). errors:
    человеко-читаемые описания. Лимит 100 валидных строк проверяется
    выше уровнем (BULK_IMPORT_TOO_MANY)."""
    entries: list[tuple[str, int]] = []
    errors: list[str] = []
    for idx, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            errors.append(f"Строка {idx}: \"{line}\" — нужно <id> <days>")
            continue
        ident, days_str = parts[0], parts[1].strip()
        try:
            days = int(days_str)
        except ValueError:
            errors.append(f"Строка {idx}: \"{line}\" — days не число")
            continue
        if days <= 0 or days > 365:
            errors.append(f"Строка {idx}: \"{line}\" — days вне 1..365")
            continue
        entries.append((ident, days))
    return entries, errors


def _format_errors_block(errors: list[str], cap: int = 5) -> str:
    if not errors:
        return ""
    head = errors[:cap]
    block = "\n" + "\n".join(f"  - {e}" for e in head)
    if len(errors) > cap:
        block += f"\n  ... и ещё {len(errors) - cap} ошибок"
    return block


async def _resolve_bulk_entries(
    bot: Bot, entries: list[tuple[str, int]],
) -> tuple[list[tuple[int, int]], list[str]]:
    """Резолвит @username → tg_id. Возвращает (resolved, errors).
    50ms пауза между bot.get_chat вызовами (rate-limit safety)."""
    resolved: list[tuple[int, int]] = []
    errors: list[str] = []
    for idx, (ident, days) in enumerate(entries, start=1):
        if ident.startswith("@"):
            tg_id, err = await _resolve_target(bot, ident)
            if tg_id is None:
                errors.append(f"Строка {idx}: \"{ident}\" — {err}")
                continue
            await asyncio.sleep(0.05)
        else:
            tg_id, err = await _resolve_target(bot, ident)
            if tg_id is None:
                errors.append(f"Строка {idx}: \"{ident}\" — {err}")
                continue
        resolved.append((tg_id, days))
    return resolved, errors


@router.message(Command("bulk_import_legacy"))
@admin_only
async def cmd_bulk_import_legacy(message: Message, state: FSMContext) -> None:
    """Slash-вход в FSM bulk_import (тот же flow что у callback admin:bulk_import)."""
    await message.answer(BULK_IMPORT_PROMPT, parse_mode="HTML")
    await state.set_state(AdminFlow.waiting_bulk_import_input)


@router.message(Command("cleanup_chats"))
@admin_only
async def cmd_cleanup_chats(message: Message, bot: Bot) -> None:
    text = await _build_cleanup_text(bot)
    await message.answer(text, parse_mode="HTML")


@router.message(Command("pending"))
@admin_only
async def cmd_pending(message: Message) -> None:
    text = await _build_pending_text()
    await message.answer(text, parse_mode="HTML")


@router.message(Command("health"))
@admin_only
async def cmd_health(message: Message, bot: Bot) -> None:
    text = await _build_health_text(bot)
    await message.answer(text, parse_mode="HTML")


# =============================================================================
# Inline-панель: callback'и
# =============================================================================

@router.callback_query(F.data == "admin:close")
async def cb_admin_close(cq: CallbackQuery) -> None:
    # close доступен всем — он только закрывает сообщение пользователя у него же.
    # Никакого state-эффекта. Защищать его аутентификацией не нужно.
    if cq.message is not None:
        try:
            await cq.message.delete()
        except Exception:
            try:
                await cq.message.edit_text("Закрыто.")
            except Exception:
                pass
    await cq.answer()


@router.callback_query(F.data == "admin:stats")
@admin_only_cb
async def cb_admin_stats(cq: CallbackQuery) -> None:
    text = await _build_stats_text()
    if cq.message is not None:
        await cq.message.answer(text, parse_mode="HTML")
    await cq.answer()


@router.callback_query(F.data == "admin:export")
@admin_only_cb
async def cb_admin_export(cq: CallbackQuery) -> None:
    await cq.answer("Генерирую файл...")
    data, count = await _generate_users_xlsx()
    filename = f"users_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.xlsx"
    if cq.message is not None:
        await cq.message.answer_document(
            BufferedInputFile(data, filename=filename),
            caption=f"📊 {count} юзеров",
        )


@router.callback_query(F.data == "admin:pending")
@admin_only_cb
async def cb_admin_pending(cq: CallbackQuery) -> None:
    text = await _build_pending_text()
    if cq.message is not None:
        await cq.message.answer(text, parse_mode="HTML")
    await cq.answer()


@router.callback_query(F.data == "admin:health")
@admin_only_cb
async def cb_admin_health(cq: CallbackQuery, bot: Bot) -> None:
    text = await _build_health_text(bot)
    if cq.message is not None:
        await cq.message.answer(text, parse_mode="HTML")
    await cq.answer()


@router.callback_query(F.data == "admin:cleanup")
@admin_only_cb
async def cb_admin_cleanup(cq: CallbackQuery, bot: Bot) -> None:
    text = await _build_cleanup_text(bot)
    if cq.message is not None:
        await cq.message.answer(text, parse_mode="HTML")
    await cq.answer()


# ---- FSM-кнопки: find / extend / revoke ------------------------------------

@router.callback_query(F.data == "admin:find")
@admin_only_cb
async def cb_admin_find_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(
            "Введи telegram_id или @username:\n\n(Или /cancel чтобы отменить.)"
        )
    await state.set_state(AdminFlow.waiting_find_query)
    await cq.answer()


@router.message(AdminFlow.waiting_find_query)
async def fsm_find_query(message: Message, state: FSMContext) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    text = await _find_user_text(message.text.strip())
    await message.answer(text, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "admin:extend")
@admin_only_cb
async def cb_admin_extend_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(
            "Введи telegram_id и кол-во дней через пробел.\n"
            "Например: <code>123456789 7</code>\n\n"
            "(Или /cancel чтобы отменить.)",
            parse_mode="HTML",
        )
    await state.set_state(AdminFlow.waiting_extend_input)
    await cq.answer()


@router.message(AdminFlow.waiting_extend_input)
async def fsm_extend_input(message: Message, state: FSMContext) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("Неверный формат. Нужно: <telegram_id> <days>")
        return  # state остаётся, повторный ввод
    try:
        tg_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        await message.answer("ID и дни должны быть числами.")
        return
    if days <= 0 or days > 365:
        await message.answer("Дней должно быть от 1 до 365.")
        return
    text = await _extend_user(tg_id, days)
    await message.answer(text, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "admin:reduce")
@admin_only_cb
async def cb_admin_reduce_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(REDUCE_PROMPT, parse_mode="HTML")
    await state.set_state(AdminFlow.waiting_reduce_input)
    await cq.answer()


@router.message(AdminFlow.waiting_reduce_input)
async def fsm_reduce_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    parsed = _parse_id_days(message.text.strip())
    if isinstance(parsed, str):
        # state остаётся — юзер пробует ещё раз
        if parsed.startswith("Usage:"):
            await message.answer("Неверный формат. Нужно: <telegram_id> <days>")
        elif "Days must be" in parsed:
            await message.answer("Дней должно быть от 1 до 365.")
        else:
            await message.answer("ID и дни должны быть числами.")
        return
    tg_id, days = parsed
    text = await _reduce_user(bot, tg_id, days)
    await message.answer(text, parse_mode="HTML")
    await state.clear()


# --- callback: import_legacy ------------------------------------------------

@router.callback_query(F.data == "admin:import_legacy")
@admin_only_cb
async def cb_admin_import_legacy_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(IMPORT_LEGACY_PROMPT, parse_mode="HTML")
    await state.set_state(AdminFlow.waiting_import_legacy_input)
    await cq.answer()


@router.message(AdminFlow.waiting_import_legacy_input)
async def fsm_import_legacy_input(
    message: Message, state: FSMContext, bot: Bot,
) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Неверный формат. Нужно: <id|@username> <days>")
        return
    ident, days_str = parts[0], parts[1].strip()
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("Days должен быть числом.")
        return
    if days <= 0 or days > 365:
        await message.answer("Days должно быть от 1 до 365.")
        return

    tg_id, err = await _resolve_target(bot, ident)
    if tg_id is None:
        await message.answer(f"❌ {err}")
        return

    text = await _import_legacy_one_text(tg_id, days)
    await message.answer(text, parse_mode="HTML")
    await state.clear()


# --- callback: grant --------------------------------------------------------

@router.callback_query(F.data == "admin:grant")
@admin_only_cb
async def cb_admin_grant_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(GRANT_PROMPT, parse_mode="HTML")
    await state.set_state(AdminFlow.waiting_grant_input)
    await cq.answer()


@router.message(AdminFlow.waiting_grant_input)
async def fsm_grant_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Неверный формат. Нужно: <id|@username> <days>")
        return
    ident, days_str = parts[0], parts[1].strip()
    try:
        days = int(days_str)
    except ValueError:
        await message.answer("Days должен быть числом.")
        return
    if days <= 0 or days > 365:
        await message.answer("Days должно быть от 1 до 365.")
        return

    tg_id, err = await _resolve_target(bot, ident)
    if tg_id is None:
        await message.answer(f"❌ {err}")
        return

    text = await _grant_user(bot, tg_id, days)
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await state.clear()


# --- callback: reissue ------------------------------------------------------

@router.callback_query(F.data == "admin:reissue")
@admin_only_cb
async def cb_admin_reissue_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(REISSUE_PROMPT, parse_mode="HTML")
    await state.set_state(AdminFlow.waiting_reissue_input)
    await cq.answer()


@router.message(AdminFlow.waiting_reissue_input)
async def fsm_reissue_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    ident = message.text.strip().split(maxsplit=1)[0]
    tg_id, err = await _resolve_target(bot, ident)
    if tg_id is None:
        await message.answer(f"❌ {err}")
        return

    text = await _reissue_user(bot, tg_id)
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await state.clear()


# --- callback + FSM: bulk_import_legacy -------------------------------------

@router.callback_query(F.data == "admin:bulk_import")
@admin_only_cb
async def cb_admin_bulk_import_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(BULK_IMPORT_PROMPT, parse_mode="HTML")
    await state.set_state(AdminFlow.waiting_bulk_import_input)
    await cq.answer()


@router.message(AdminFlow.waiting_bulk_import_input)
async def fsm_bulk_import_input(
    message: Message, state: FSMContext, bot: Bot,
) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return

    entries, parse_errors = _parse_bulk_import_lines(message.text)
    if len(entries) > BULK_IMPORT_MAX_LINES:
        await message.answer(BULK_IMPORT_TOO_MANY.format(n=len(entries)))
        await state.clear()
        return

    if not entries and not parse_errors:
        await message.answer(BULK_IMPORT_NOTHING_TO_IMPORT)
        await state.clear()
        return

    # Резолв @username — может быть медленно; уведомим если их много.
    usernames = sum(1 for ident, _ in entries if ident.startswith("@"))
    if usernames > 5:
        await message.answer(BULK_IMPORT_RESOLVING.format(n=usernames))

    resolved, resolve_errors = await _resolve_bulk_entries(bot, entries)
    all_errors = parse_errors + resolve_errors

    if not resolved:
        await message.answer(
            BULK_IMPORT_NOTHING_TO_IMPORT
            + (_format_errors_block(all_errors, cap=10) if all_errors else "")
        )
        await state.clear()
        return

    # Preview: считаем, сколько новых vs существующих.
    pool = get_pool()
    tg_ids = [tg for tg, _ in resolved]
    existing_rows = await pool.fetch(
        "SELECT telegram_id FROM users WHERE telegram_id = ANY($1::bigint[])",
        tg_ids,
    )
    existing_ids = {r["telegram_id"] for r in existing_rows}
    new_count = sum(1 for tg in tg_ids if tg not in existing_ids)
    existing_count = len(resolved) - new_count

    preview = BULK_IMPORT_PREVIEW.format(
        ok=len(resolved),
        new=new_count,
        existing=existing_count,
        errors=len(all_errors),
        errors_block=_format_errors_block(all_errors),
    )
    # Telegram limit — 4096 символов. Обрезаем хвост на всякий случай.
    if len(preview) > 4000:
        preview = preview[:3996] + "\n..."

    await state.set_state(AdminFlow.waiting_bulk_import_confirm)
    await state.update_data(resolved=resolved, errors=all_errors)

    await message.answer(
        preview, parse_mode="HTML", reply_markup=bulk_import_confirm_kb(),
    )


@router.callback_query(F.data == "admin:bulk_import_confirm")
@admin_only_cb
async def cb_admin_bulk_import_confirm(
    cq: CallbackQuery, state: FSMContext,
) -> None:
    data = await state.get_data()
    resolved: list[tuple[int, int]] = data.get("resolved") or []
    parse_errors: list[str] = data.get("errors") or []

    if not resolved:
        if cq.message is not None:
            await cq.message.answer(BULK_IMPORT_NOTHING_TO_IMPORT)
        await state.clear()
        await cq.answer()
        return

    await cq.answer("Запускаю импорт...")

    ok = 0
    new_count = 0
    existing_count = 0
    granted_total = 0
    runtime_errors: list[str] = []

    for tg_id, days in resolved:
        try:
            result = await import_legacy_user(tg_id, days, settings.chat_ids)
            ok += 1
            granted_total += result.granted_count
            if result.was_created:
                new_count += 1
            else:
                existing_count += 1
        except Exception as e:
            log.error("bulk_import tg=%s days=%s failed: %s", tg_id, days, e)
            runtime_errors.append(f"tg={tg_id}: {e}")
        await asyncio.sleep(0.1)

    final = BULK_IMPORT_DONE.format(
        ok=ok, new=new_count, existing=existing_count,
        granted=granted_total,
        errors=len(parse_errors) + len(runtime_errors),
    )
    if runtime_errors:
        final += _format_errors_block(runtime_errors, cap=5)
    if cq.message is not None:
        await cq.message.answer(final, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "admin:revoke")
@admin_only_cb
async def cb_admin_revoke_start(cq: CallbackQuery, state: FSMContext) -> None:
    if cq.message is not None:
        await cq.message.answer(
            "⚠️ Эта команда КИКНЕТ юзера из всех каналов и завершит его подписку.\n\n"
            "Введи telegram_id (или /cancel):"
        )
    await state.set_state(AdminFlow.waiting_revoke_id)
    await cq.answer()


@router.message(AdminFlow.waiting_revoke_id)
async def fsm_revoke_id(message: Message, state: FSMContext) -> None:
    if (
        message.from_user is None
        or message.from_user.id != settings.admin_telegram_id
    ):
        return
    if not message.text:
        return
    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return

    user_text = await _find_user_text(str(tg_id))
    await message.answer(
        f"{user_text}\n\n<b>Точно отозвать этого юзера?</b>",
        reply_markup=revoke_confirm_kb(tg_id),
        parse_mode="HTML",
    )
    await state.clear()


@router.callback_query(F.data.startswith("admin:revoke_confirm:"))
@admin_only_cb
async def cb_admin_revoke_confirm(cq: CallbackQuery, bot: Bot) -> None:
    if cq.data is None:
        await cq.answer()
        return
    parts = cq.data.split(":")
    if len(parts) != 3:
        await cq.answer("Bad payload", show_alert=True)
        return
    try:
        tg_id = int(parts[2])
    except ValueError:
        await cq.answer("Bad telegram_id", show_alert=True)
        return
    text = await _revoke_user(bot, tg_id)
    if cq.message is not None:
        await cq.message.answer(text, parse_mode="HTML")
    await cq.answer("Отозвано")


# ---- /cancel — выйти из любого FSM-state админки ---------------------------

@router.message(
    StateFilter(
        AdminFlow.waiting_find_query,
        AdminFlow.waiting_extend_input,
        AdminFlow.waiting_reduce_input,
        AdminFlow.waiting_revoke_id,
        AdminFlow.waiting_import_legacy_input,
        AdminFlow.waiting_bulk_import_input,
        AdminFlow.waiting_bulk_import_confirm,
        AdminFlow.waiting_grant_input,
        AdminFlow.waiting_reissue_input,
    ),
    Command("cancel"),
)
async def fsm_cancel_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")
