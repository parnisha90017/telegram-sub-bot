from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import main_menu_kb, plans_kb
from app.bot.texts import (
    ABOUT_US,
    SELECT_PLAN,
    WELCOME_ACTIVE,
    WELCOME_EXPIRED,
    WELCOME_NEW,
)
from app.db.queries import get_user_by_tg_id, upsert_user

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
