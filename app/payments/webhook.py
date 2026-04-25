from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from aiocryptopay import AioCryptoPay
from aiocryptopay.models.update import Update
from aiogram import Bot

from app.chats.manager import issue_invite_links_and_send, unban_from_all_chats
from app.db.queries import process_paid_invoice

log = logging.getLogger(__name__)


def register_cryptopay_handlers(crypto: AioCryptoPay, bot: Bot) -> None:
    @crypto.pay_handler()
    async def on_invoice_paid(update: Update, _app: object) -> None:
        invoice = update.payload
        invoice_id = str(invoice.invoice_id)

        if getattr(invoice, "status", None) != "paid":
            log.info(
                "invoice %s arrived with status=%s — skipping",
                invoice_id, invoice.status,
            )
            return

        try:
            webhook_amount = Decimal(str(invoice.amount))
        except (InvalidOperation, TypeError):
            log.error("invoice %s has unparseable amount %r", invoice_id, invoice.amount)
            return

        payment = await process_paid_invoice(
            provider="cryptobot",
            payment_id=invoice_id,
            webhook_amount=webhook_amount,
        )
        if payment is None:
            log.info(
                "invoice %s: no-op (unknown / already paid / amount mismatch)",
                invoice_id,
            )
            return

        telegram_id = payment["telegram_id"]
        paid_until = payment["paid_until"]
        log.info("invoice %s: subscription extended for tg=%s", invoice_id, telegram_id)
        await unban_from_all_chats(bot, telegram_id)
        await issue_invite_links_and_send(bot, telegram_id, paid_until)
