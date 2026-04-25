from __future__ import annotations

import logging
from typing import Callable

from aiogram import Bot
from aiohttp import web

from app.chats.manager import issue_invite_links_and_send, unban_from_all_chats
from app.db.queries import process_paid_invoice
from app.payments.base import PaymentProvider

logger = logging.getLogger(__name__)


def make_heleket_handler(provider: PaymentProvider, bot: Bot) -> Callable:
    """Builds an aiohttp handler for Heleket webhook.

    Pipeline: read raw bytes → verify HMAC sign → if status=paid →
    process_paid_invoice (atomic mark+extend) → unban + invite links.

    Always returns 200 on signature-valid + business-decision states
    (already processed / unknown invoice / non-paid status) so Heleket
    stops retrying. Returns 401 on bad signature, 500 on DB failure.
    """

    async def handler(request: web.Request) -> web.Response:
        body_bytes = await request.read()
        valid, event = provider.verify_webhook(body_bytes, dict(request.headers))

        if not valid or event is None:
            logger.warning("Heleket webhook: invalid sign or unparseable body")
            return web.json_response({"error": "invalid_sign"}, status=401)

        if event.status != "paid":
            logger.info(
                "Heleket webhook: invoice=%s status=%s — skip",
                event.invoice_id, event.status,
            )
            return web.json_response({"ok": True, "ignored": event.status})

        try:
            payment = await process_paid_invoice(
                provider="heleket",
                payment_id=event.invoice_id,
                webhook_amount=event.amount_usd,
            )
        except Exception as e:
            logger.exception(
                "Heleket webhook: process_paid_invoice failed for invoice=%s: %s",
                event.invoice_id, e,
            )
            return web.json_response({"error": "processing_failed"}, status=500)

        if payment is None:
            logger.info(
                "Heleket webhook: invoice=%s no-op "
                "(unknown / already paid / amount mismatch)",
                event.invoice_id,
            )
            return web.json_response({"ok": True})

        telegram_id = payment["telegram_id"]
        paid_until = payment["paid_until"]
        logger.info(
            "Heleket webhook: invoice=%s → subscription extended for tg=%s",
            event.invoice_id, telegram_id,
        )

        try:
            await unban_from_all_chats(bot, telegram_id)
            await issue_invite_links_and_send(bot, telegram_id, paid_until)
        except Exception as e:
            # Платёж уже зафиксирован в БД, но пользователю не выдали ссылки.
            # Логируем для ручного разбора. Webhook возвращает 200, чтобы Heleket не ретраил.
            logger.exception(
                "Heleket webhook: post-payment actions failed for tg=%s: %s",
                telegram_id, e,
            )

        return web.json_response({"ok": True})

    return handler
