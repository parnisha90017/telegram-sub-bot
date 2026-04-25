from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.texts import REMIND_24H
from app.db.queries import (
    expire_and_return_ids,
    list_expired_granted,
    list_expiring_between,
    mark_revoked,
)

log = logging.getLogger(__name__)


async def remind_24h(bot: Bot) -> None:
    """Уведомление за ~24 часа. Текст содержит РЕАЛЬНУЮ дату paid_until и
    остаток в часах — никакого {plan}. Тариф в reminder'е вводит в
    заблуждение: при продлении 7+3 дня plan хранится последний (3д), но
    остаток времени = 10 дней."""
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=23, minutes=30)
    window_end = now + timedelta(hours=24, minutes=30)

    rows = await list_expiring_between(window_start, window_end)
    log.info("remind_24h: %d users to notify", len(rows))

    for row in rows:
        tg_id = row["telegram_id"]
        paid_until: datetime = row["paid_until"]
        hours_left = max(0, int((paid_until - datetime.now(timezone.utc)).total_seconds() // 3600))
        text = REMIND_24H.format(
            paid_until_str=paid_until.strftime("%d.%m.%Y %H:%M UTC"),
            hours_left=hours_left,
        )

        try:
            await bot.send_message(tg_id, text)
        except TelegramForbiddenError:
            pass
        except TelegramRetryAfter as e:
            try:
                await asyncio.sleep(float(e.retry_after))
                await bot.send_message(tg_id, text)
            except Exception as exc:
                log.warning("remind retry to tg=%s failed: %s", tg_id, exc)
        except Exception as e:
            log.warning("remind to tg=%s failed: %s", tg_id, e)


async def kick_expired(bot: Bot) -> None:
    """Кикает реальных участников каналов из granted_access (joined_at IS NOT
    NULL, paid_until < NOW, revoked_at IS NULL). Затем синхронно помечает
    users.status='expired' для UI/статистики."""
    expired = await list_expired_granted(limit=100)
    if expired:
        log.info("kick_expired: %d granted_access rows to revoke", len(expired))

    for row in expired:
        chat_id = row["chat_id"]
        tg_id = row["telegram_id"]
        granted_id = row["id"]
        # source может отсутствовать в моках старых тестов — терпимо
        source = row.get("source") if isinstance(row, dict) else None
        try:
            await bot.ban_chat_member(chat_id=chat_id, user_id=tg_id)
            await asyncio.sleep(0.05)
            await bot.unban_chat_member(chat_id=chat_id, user_id=tg_id)
        except TelegramBadRequest as e:
            # Не в чате / уже забанен / нет прав — фиксируем revoked.
            log.info(
                "kick tg=%s chat=%s source=%s bad_request (treated as revoked): %s",
                tg_id, chat_id, source, e,
            )
        except TelegramRetryAfter as e:
            log.warning(
                "kick tg=%s chat=%s source=%s rate-limited: retry_after=%s",
                tg_id, chat_id, source, e.retry_after,
            )
            continue
        except Exception as e:
            log.error("kick tg=%s chat=%s source=%s failed: %s", tg_id, chat_id, source, e)
            continue

        try:
            await mark_revoked(granted_id)
            log.info(
                "revoked granted_access id=%s tg=%s chat=%s source=%s",
                granted_id, tg_id, chat_id, source,
            )
        except Exception as e:
            log.error("mark_revoked id=%s failed: %s", granted_id, e)

        await asyncio.sleep(0.05)

    # Параллельно помечаем users как expired (для статистики/UI). Возвращаемый
    # список не используем — kick идёт по granted_access выше.
    await expire_and_return_ids()


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        remind_24h, "interval", hours=1, args=[bot],
        id="remind_24h", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        kick_expired, "interval", minutes=10, args=[bot],
        id="kick_expired", max_instances=1, coalesce=True,
    )
    return scheduler
