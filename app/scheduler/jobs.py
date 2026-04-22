from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.texts import REMIND_24H
from app.chats.manager import kick_from_all_chats
from app.db.queries import expire_and_return_ids, list_expiring_between

log = logging.getLogger(__name__)


async def remind_24h(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    window_start = now + timedelta(hours=23, minutes=30)
    window_end = now + timedelta(hours=24, minutes=30)

    tg_ids = await list_expiring_between(window_start, window_end)
    log.info("remind_24h: %d users to notify", len(tg_ids))

    for tg_id in tg_ids:
        try:
            await bot.send_message(tg_id, REMIND_24H)
        except TelegramForbiddenError:
            pass
        except TelegramRetryAfter as e:
            try:
                import asyncio

                await asyncio.sleep(float(e.retry_after))
                await bot.send_message(tg_id, REMIND_24H)
            except Exception as exc:
                log.warning("remind retry to tg=%s failed: %s", tg_id, exc)
        except Exception as e:
            log.warning("remind to tg=%s failed: %s", tg_id, e)


async def kick_expired(bot: Bot) -> None:
    tg_ids = await expire_and_return_ids()
    if not tg_ids:
        return
    log.info("kick_expired: expiring %d users", len(tg_ids))
    for tg_id in tg_ids:
        await kick_from_all_chats(bot, tg_id)


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
