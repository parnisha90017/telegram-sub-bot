from __future__ import annotations

import logging

from aiocryptopay import AioCryptoPay
from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards import PLANS
from app.bot.texts import INVOICE_CREATED, INVOICE_ERROR
from app.db.queries import insert_pending_payment, upsert_user
from app.payments.cryptopay import create_invoice_for

log = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(cq: CallbackQuery, crypto: AioCryptoPay) -> None:
    if cq.data is None or cq.from_user is None or cq.message is None:
        await cq.answer()
        return

    plan_key = cq.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await cq.answer("Неизвестный тариф", show_alert=True)
        return

    plan = PLANS[plan_key]

    await upsert_user(cq.from_user.id, cq.from_user.username)

    try:
        invoice_id, url = await create_invoice_for(crypto, cq.from_user.id, plan_key)
    except Exception as e:
        log.exception("create_invoice failed for tg=%s plan=%s: %s", cq.from_user.id, plan_key, e)
        await cq.message.answer(INVOICE_ERROR)
        await cq.answer()
        return

    await insert_pending_payment(
        telegram_id=cq.from_user.id,
        plan=plan_key,
        amount=int(plan["amount"]),
        payment_id=str(invoice_id),
    )

    await cq.message.answer(
        INVOICE_CREATED.format(amount=plan["amount"], url=url),
        disable_web_page_preview=True,
    )
    await cq.answer()
