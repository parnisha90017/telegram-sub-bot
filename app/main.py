from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.bot.handlers import admin as admin_h
from app.bot.handlers import join_request as join_request_h
from app.bot.handlers import payment as payment_h
from app.bot.handlers import start as start_h
from app.chats.manager import check_bot_admin_rights
from app.config import Settings, settings
from app.db.pool import close_pool, init_pool
from app.payments.base import PaymentProvider
from app.payments.cryptopay import build_client
from app.payments.cryptopay_provider import CryptoPayProvider
from app.payments.heleket import HeleketProvider
from app.payments.webhook import register_cryptopay_handlers
from app.scheduler.jobs import setup_scheduler
from app.web.heleket_webhook import make_heleket_handler

log = logging.getLogger(__name__)


def _build_providers(s: Settings, crypto) -> dict[str, PaymentProvider]:
    providers: dict[str, PaymentProvider] = {}

    if "cryptobot" in s.enabled_providers:
        providers["cryptobot"] = CryptoPayProvider(crypto, bot_username=s.bot_username)
        log.info("Provider enabled: cryptobot")

    if "heleket" in s.enabled_providers:
        if s.heleket_merchant_uuid and s.heleket_api_key:
            callback = s.heleket_webhook_url
            if not callback:
                if s.telegram_webhook_url:
                    base = s.telegram_webhook_url.rsplit("/", 1)[0]
                    callback = f"{base.rstrip('/')}{s.heleket_webhook_path}"
            if not callback:
                log.warning(
                    "heleket enabled but no callback URL "
                    "(set HELEKET_WEBHOOK_URL or TELEGRAM_WEBHOOK_URL) — skipping"
                )
            else:
                providers["heleket"] = HeleketProvider(
                    merchant_uuid=s.heleket_merchant_uuid,
                    api_key=s.heleket_api_key,
                    callback_url=callback,
                )
                log.info("Provider enabled: heleket (callback=%s)", callback)
        else:
            log.warning(
                "heleket in ENABLED_PROVIDERS but "
                "HELEKET_MERCHANT_UUID / HELEKET_API_KEY missing — skipping"
            )

    return providers


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


def build_app() -> web.Application:
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    # admin_h ДО start_h: команды (/find, /export, /stats, ...) должны
    # перехватываться раньше, чем общие message handlers (на будущее).
    dp.include_router(admin_h.router)
    dp.include_router(start_h.router)
    dp.include_router(payment_h.router)
    dp.include_router(join_request_h.router)

    async def _on_tg_startup(bot: Bot) -> None:
        if not settings.telegram_webhook_url:
            log.warning("TELEGRAM_WEBHOOK_URL is empty — skipping set_webhook")
            return
        # allowed_updates обязателен, чтобы Telegram доставлял chat_join_request
        # (по дефолту он включён, но явный список безопаснее: при смене dp в
        # будущем не потеряем тип update'а).
        await bot.set_webhook(
            url=settings.telegram_webhook_url,
            secret_token=settings.telegram_webhook_secret,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info(
            "Telegram webhook set to %s (allowed_updates=%s)",
            settings.telegram_webhook_url, dp.resolve_used_update_types(),
        )

    async def _on_tg_shutdown(bot: Bot) -> None:
        if settings.telegram_webhook_url:
            try:
                await bot.delete_webhook()
            except Exception as e:
                log.warning("delete_webhook failed: %s", e)

    dp.startup.register(_on_tg_startup)
    dp.shutdown.register(_on_tg_shutdown)

    crypto = build_client()
    providers = _build_providers(settings, crypto)
    if not providers:
        raise RuntimeError("No payment providers configured")
    log.info("Active providers: %s", list(providers.keys()))

    dp["providers"] = providers
    dp["settings"] = settings

    if "cryptobot" in providers:
        register_cryptopay_handlers(crypto, bot)

    app = web.Application()
    app.router.add_get("/health", health)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.telegram_webhook_secret,
    ).register(app, path="/tg/webhook")

    if "cryptobot" in providers:
        app.router.add_post("/cryptopay/webhook", crypto.get_updates)
        log.info("Mounted cryptobot webhook at /cryptopay/webhook")

    if "heleket" in providers:
        app.router.add_post(
            settings.heleket_webhook_path,
            make_heleket_handler(providers["heleket"], bot),
        )
        log.info("Mounted heleket webhook at %s", settings.heleket_webhook_path)

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
