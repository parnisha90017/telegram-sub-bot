from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.handlers import payment as payment_h
from app.bot.handlers import start as start_h
from app.chats.manager import check_bot_admin_rights
from app.config import settings
from app.db.pool import close_pool, init_pool
from app.payments.cryptopay import build_client
from app.payments.webhook import register_cryptopay_handlers
from app.scheduler.jobs import setup_scheduler

log = logging.getLogger(__name__)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _on_tg_startup(bot: Bot) -> None:
    if not settings.telegram_webhook_url:
        log.warning("TELEGRAM_WEBHOOK_URL is empty — skipping set_webhook")
        return
    await bot.set_webhook(
        url=settings.telegram_webhook_url,
        secret_token=settings.telegram_webhook_secret,
        drop_pending_updates=True,
    )
    log.info("Telegram webhook set to %s", settings.telegram_webhook_url)


async def _on_tg_shutdown(bot: Bot) -> None:
    if settings.telegram_webhook_url:
        try:
            await bot.delete_webhook()
        except Exception as e:
            log.warning("delete_webhook failed: %s", e)


def build_app() -> web.Application:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(start_h.router)
    dp.include_router(payment_h.router)
    dp.startup.register(_on_tg_startup)
    dp.shutdown.register(_on_tg_shutdown)

    crypto = build_client()
    dp["crypto"] = crypto
    register_cryptopay_handlers(crypto, bot)

    app = web.Application()
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.telegram_webhook_secret,
    ).register(app, path="/tg/webhook")

    app.router.add_post("/cryptopay/webhook", crypto.get_updates)

    scheduler = setup_scheduler(bot)

    async def on_startup(_: web.Application) -> None:
        await init_pool()
        log.info("DB pool initialized")
        await check_bot_admin_rights(bot)
        scheduler.start()
        log.info("Scheduler started")

    async def on_cleanup(_: web.Application) -> None:
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            log.warning("scheduler.shutdown failed: %s", e)
        try:
            await crypto.close()
        except Exception as e:
            log.warning("crypto.close failed: %s", e)
        await close_pool()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    setup_application(app, dp, bot=bot)
    return app


def main() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(build_app(), host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
