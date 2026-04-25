from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.bot.texts import PAID_WITH_LINKS
from app.config import settings
from app.db.queries import grant_access

log = logging.getLogger(__name__)


async def check_bot_admin_rights(bot: Bot) -> None:
    """Предстарт-check: бот должен быть админом с нужными правами во всех 4 чатах.

    При отсутствии прав — WARN в лог. Не падаем, т.к. одна-две настройки
    могут быть поправлены после запуска.
    """
    me = await bot.get_me()
    for chat_id in settings.chat_ids:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
        except Exception as e:
            log.warning("chat=%s: cannot get_chat_member for bot: %s", chat_id, e)
            continue
        if member.status not in ("administrator", "creator"):
            log.warning("chat=%s: bot is not admin (status=%s)", chat_id, member.status)
            continue
        if member.status == "administrator":
            if not getattr(member, "can_invite_users", False):
                log.warning("chat=%s: bot lacks can_invite_users", chat_id)
            if not getattr(member, "can_restrict_members", False):
                log.warning("chat=%s: bot lacks can_restrict_members", chat_id)


async def unban_from_all_chats(bot: Bot, telegram_id: int) -> None:
    """Снимает бан во всех 4 чатах, если пользователь в ЧС (например,
    был забанен вручную админом). only_if_banned=True — защита от
    побочных эффектов для тех, кто сейчас в чате: без этого флага
    unban_chat_member удалит пользователя из чата."""
    for chat_id in settings.chat_ids:
        try:
            await bot.unban_chat_member(
                chat_id=chat_id,
                user_id=telegram_id,
                only_if_banned=True,
            )
        except Exception as e:
            log.info("unban tg=%s chat=%s skipped: %s", telegram_id, chat_id, e)
        await asyncio.sleep(0.1)


async def issue_invite_links_and_send(
    bot: Bot,
    telegram_id: int,
    paid_until: datetime,
) -> None:
    """Создаёт invite-ссылки во всех 4 чатах с creates_join_request=True,
    регистрирует каждую в granted_access (привязка к покупателю), затем
    отправляет ссылки в ЛС покупателя.

    member_limit при creates_join_request=True указывать НЕЛЬЗЯ — Telegram
    отвергает (см. Bot API: "If creates_join_request is True, member_limit
    can't be specified"). Защита от перепродажи доступа теперь в
    ChatJoinRequest-handler'е (см. app/bot/handlers/join_request.py).
    """
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
            log.error("create_chat_invite_link for chat=%s failed: %s", chat_id, e)
            continue

        try:
            await grant_access(telegram_id, chat_id, link.invite_link, paid_until)
        except Exception as e:
            log.error(
                "grant_access for tg=%s chat=%s link=%s failed: %s",
                telegram_id, chat_id, link.invite_link, e,
            )
            # Не отдаём ссылку юзеру: без записи в granted_access
            # join-request будет declined.
            continue

        links.append(link.invite_link)

    if not links:
        log.error("no invite links generated for tg=%s", telegram_id)
        return

    text = PAID_WITH_LINKS.format(
        links="\n".join(f"{i+1}. {url}" for i, url in enumerate(links))
    )
    try:
        await bot.send_message(telegram_id, text, disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.warning("tg=%s blocked the bot; links lost", telegram_id)
    except Exception as e:
        log.error("send_message to tg=%s failed: %s", telegram_id, e)


async def kick_from_all_chats(bot: Bot, telegram_id: int) -> None:
    for chat_id in settings.chat_ids:
        await _kick_one(bot, chat_id, telegram_id)
        await asyncio.sleep(0.1)


async def _kick_one(bot: Bot, chat_id: int, telegram_id: int) -> None:
    for attempt in range(2):
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=telegram_id)
            await bot.unban_chat_member(chat_id=chat_id, user_id=telegram_id)
            return
        except TelegramBadRequest as e:
            log.info("kick tg=%s chat=%s bad_request: %s", telegram_id, chat_id, e)
            return
        except TelegramRetryAfter as e:
            if attempt == 0:
                await asyncio.sleep(float(e.retry_after))
                continue
            log.error("kick tg=%s chat=%s still rate-limited", telegram_id, chat_id)
            return
        except Exception as e:
            log.error("kick tg=%s chat=%s failed: %s", telegram_id, chat_id, e)
            return
