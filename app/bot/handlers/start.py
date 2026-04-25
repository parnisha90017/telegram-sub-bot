from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import SUPPORT_URL, main_menu_kb, my_subscription_kb, plans_kb
from app.bot.texts import (
    ABOUT_US,
    MY_REISSUE_ERROR,
    MY_REISSUE_EXPIRED,
    MY_REISSUE_OK,
    MY_REISSUE_RATE_LIMITED,
    MY_SUBSCRIPTION_ACTIVE,
    MY_SUBSCRIPTION_NONE,
    SELECT_PLAN,
    WELCOME_ACTIVE,
    WELCOME_EXPIRED,
    WELCOME_NEW,
)
from app.chats.manager import issue_invite_links_and_send
from app.db.queries import (
    get_reissue_status,
    get_user_by_tg_id,
    perform_user_reissue_atomic,
    upsert_user,
)

log = logging.getLogger(__name__)

REISSUE_RATE_LIMIT = 3600  # секунд между запросами одного юзера

router = Router()


def _format_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M")


async def _build_menu(telegram_id: int, username: str | None) -> tuple[str, bool]:
    await upsert_user(telegram_id, username)
    row = await get_user_by_tg_id(telegram_id)
    is_active = bool(row and row["status"] == "active")

    text = WELCOME_NEW
    if row and row["paid_until"] is not None:
        now = datetime.now(timezone.utc)
        if row["paid_until"] > now:
            text = WELCOME_ACTIVE.format(paid_until=_format_dt(row["paid_until"]))
        else:
            text = WELCOME_EXPIRED
    return text, is_active


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    text, is_active = await _build_menu(user.id, user.username)
    await message.answer(text, reply_markup=main_menu_kb(is_active))


@router.callback_query(F.data == "show_plans")
async def on_show_plans(cq: CallbackQuery) -> None:
    if not isinstance(cq.message, Message):
        await cq.answer()
        return
    try:
        await cq.message.edit_text(SELECT_PLAN, reply_markup=plans_kb())
    except Exception:
        await cq.message.answer(SELECT_PLAN, reply_markup=plans_kb())
    await cq.answer()


@router.callback_query(F.data == "back_to_menu")
async def on_back_to_menu(cq: CallbackQuery) -> None:
    if cq.from_user is None or not isinstance(cq.message, Message):
        await cq.answer()
        return
    text, is_active = await _build_menu(cq.from_user.id, cq.from_user.username)
    try:
        await cq.message.edit_text(text, reply_markup=main_menu_kb(is_active))
    except Exception:
        await cq.message.answer(text, reply_markup=main_menu_kb(is_active))
    await cq.answer()


@router.callback_query(F.data == "about")
async def on_about(cq: CallbackQuery) -> None:
    if cq.message is None:
        await cq.answer()
        return
    await cq.message.answer(ABOUT_US)
    await cq.answer()


async def _build_my_subscription(telegram_id: int) -> tuple[str, bool]:
    """Возвращает (text, is_active). is_active=True только если подписка
    действительно валидна сейчас (paid_until > NOW()). Используется для
    выбора клавиатуры (кнопка reissue показывается только активным)."""
    user = await get_user_by_tg_id(telegram_id)
    now = datetime.now(timezone.utc)
    if not user or user["paid_until"] is None or user["paid_until"] <= now:
        return MY_SUBSCRIPTION_NONE, False

    paid_until = user["paid_until"]
    delta = paid_until - now
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        remaining_str = f"{days} дн. {hours} ч."
    elif hours > 0:
        remaining_str = f"{hours} ч."
    else:
        minutes = max(1, delta.seconds // 60)
        remaining_str = f"{minutes} мин."

    text = MY_SUBSCRIPTION_ACTIVE.format(
        paid_until_str=paid_until.strftime("%d.%m.%Y %H:%M UTC"),
        remaining_str=remaining_str,
    )
    return text, True


async def _render_my_subscription(telegram_id: int) -> str:
    """Backward-compat wrapper: тесты и устаревший код ожидают str."""
    text, _ = await _build_my_subscription(telegram_id)
    return text


@router.message(Command("my"))
async def cmd_my(message: Message) -> None:
    if message.from_user is None:
        return
    text, is_active = await _build_my_subscription(message.from_user.id)
    await message.answer(text, reply_markup=my_subscription_kb(is_active))


@router.callback_query(F.data == "show_my")
async def cb_my(cq: CallbackQuery) -> None:
    if cq.from_user is None or cq.message is None:
        await cq.answer()
        return
    text, is_active = await _build_my_subscription(cq.from_user.id)
    await cq.message.answer(text, reply_markup=my_subscription_kb(is_active))
    await cq.answer()


@router.callback_query(F.data == "user:reissue_links")
async def on_reissue_links(cq: CallbackQuery, bot: Bot) -> None:
    """Юзер сам запросил перевыдачу invite-ссылок (после kick из чата
    Telethon-скриптом или просто потерял прошлые). Поток:
      1. SELECT users → есть, paid_until > NOW.
      2. Rate-limit: last_reissue_at < NOW - 1ч.
      3. Транзакция: revoke все active granted_access + UPDATE last_reissue_at.
      4. issue_invite_links_and_send(bot, tg_id, paid_until) — создаёт
         новые ссылки и шлёт юзеру в ЛС. Если упадёт после revoke —
         юзеру MY_REISSUE_ERROR, в логе ERROR (revoke уже сделан,
         юзеру можно попробовать через час).
    Anti-share держится в chat_join_request handler — друг с этой ссылкой
    не пройдёт (там сверка from_user.id == granted_access.telegram_id).
    """
    user = cq.from_user
    if user is None:
        await cq.answer()
        return

    paid_until, last_reissue_at = await get_reissue_status(user.id)
    now = datetime.now(timezone.utc)

    if paid_until is None or paid_until <= now:
        await cq.answer(MY_REISSUE_EXPIRED, show_alert=True)
        return

    if last_reissue_at is not None:
        elapsed = (now - last_reissue_at).total_seconds()
        if elapsed < REISSUE_RATE_LIMIT:
            minutes_left = max(1, int((REISSUE_RATE_LIMIT - elapsed) // 60) + 1)
            await cq.answer(
                MY_REISSUE_RATE_LIMITED.format(minutes=minutes_left),
                show_alert=True,
            )
            return

    await perform_user_reissue_atomic(user.id)

    try:
        await issue_invite_links_and_send(bot, user.id, paid_until)
    except Exception as e:
        log.error("reissue: issue_invite_links_and_send failed for tg=%s: %s", user.id, e)
        await cq.answer(
            MY_REISSUE_ERROR.format(support_url=SUPPORT_URL),
            show_alert=True,
        )
        return

    await cq.answer(MY_REISSUE_OK, show_alert=True)
