from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import PLANS, provider_pick_kb
from app.bot.texts import INVOICE_CREATED, INVOICE_ERROR, SELECT_PROVIDER
from app.config import Settings
from app.db.queries import (
    find_active_pending_payment,
    upsert_pending_payment,
    upsert_user,
)
from app.payments.base import PaymentProvider
from app.payments.cryptopay import encode_payload

log = logging.getLogger(__name__)
router = Router()

# Heleket-специфика: order_id у нас детерминированный (<tg>_<plan>) → Heleket
# по нему всегда возвращает один и тот же uuid. Кэшировать pay_url у себя в БД
# нельзя: на стороне Heleket инвойс может умереть до истечения нашего TTL,
# и юзер увидит «срок действия подтверждён» по протухшему URL. Поэтому для
# Heleket каждый клик = create_invoice(is_refresh=True). По доке: для активного
# инвойса is_refresh игнорируется (тот же uuid+url дёшево), для истёкшего —
# обновляет address+expired_at, URL гарантированно живой.
# CryptoBot этой проблемой не страдает: он генерит уникальный invoice_id на
# каждый createInvoice, поэтому для него оставляем кэш через find_active.
HELEKET = "heleket"


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(cq: CallbackQuery, settings: Settings) -> None:
    """Шаг 1: пользователь выбрал тариф → показываем выбор провайдера."""
    if cq.data is None or cq.from_user is None or not isinstance(cq.message, Message):
        await cq.answer()
        return

    plan_key = cq.data.split(":", 1)[1]
    if plan_key not in PLANS:
        await cq.answer("Неизвестный тариф", show_alert=True)
        return

    plan = PLANS[plan_key]
    text = (
        f"{plan['title']}\n\n"
        f"Сумма: {plan['amount']} USDT\n\n"
        f"{SELECT_PROVIDER}"
    )
    kb = provider_pick_kb(plan_key, settings.enabled_providers)
    try:
        await cq.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cq.message.answer(text, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("pay:"))
async def on_pay(
    cq: CallbackQuery,
    providers: dict[str, PaymentProvider],
) -> None:
    """Шаг 2: пользователь выбрал провайдера → создаём инвойс через него."""
    if cq.data is None or cq.from_user is None or not isinstance(cq.message, Message):
        await cq.answer()
        return

    parts = cq.data.split(":")
    if len(parts) != 3:
        await cq.answer("Bad callback", show_alert=True)
        return

    _, plan_key, provider_code = parts
    if plan_key not in PLANS:
        await cq.answer("Неизвестный тариф", show_alert=True)
        return
    if provider_code not in providers:
        await cq.answer("Способ оплаты сейчас недоступен", show_alert=True)
        return

    plan = PLANS[plan_key]
    provider = providers[provider_code]

    await upsert_user(cq.from_user.id, cq.from_user.username)

    if provider_code == HELEKET:
        # Heleket: всегда дёргаем create_invoice с is_refresh=True. Без кэша
        # find_active — see module-level комментарий выше про мёртвый pay_url.
        # ON CONFLICT в upsert закрывает race и кейс «тот же uuid».
        try:
            invoice = await provider.create_invoice(
                amount_usd=float(plan["amount"]),
                order_id=encode_payload(cq.from_user.id, plan_key),
                description=f"Подписка — {plan['title']}",
                is_refresh=True,
            )
        except Exception as e:
            log.exception(
                "create_invoice failed for tg=%s plan=%s provider=%s: %s",
                cq.from_user.id, plan_key, provider_code, e,
            )
            await cq.message.answer(INVOICE_ERROR)
            await cq.answer()
            return

        await upsert_pending_payment(
            telegram_id=cq.from_user.id,
            plan=plan_key,
            amount=int(plan["amount"]),
            payment_id=invoice.invoice_id,
            provider=provider_code,
            pay_url=invoice.pay_url,
        )
        log.info(
            "heleket invoice issued (is_refresh=True) tg=%s plan=%s payment_id=%s",
            cq.from_user.id, plan_key, invoice.invoice_id,
        )

        text = INVOICE_CREATED.format(amount=plan["amount"], url=invoice.pay_url)
        try:
            await cq.message.edit_text(text, disable_web_page_preview=True)
        except Exception:
            await cq.message.answer(text, disable_web_page_preview=True)
        await cq.answer()
        return

    # Прочие провайдеры (CryptoBot и т.п.): инвойс_id уникален per-call,
    # детерминированности по order_id нет → можно безопасно кэшировать
    # pay_url у нас на 1 час.
    existing = await find_active_pending_payment(
        telegram_id=cq.from_user.id,
        plan=plan_key,
        provider=provider_code,
    )
    if existing is not None:
        log.info(
            "reused existing pending invoice tg=%s plan=%s provider=%s payment_id=%s",
            cq.from_user.id, plan_key, provider_code, existing["payment_id"],
        )
        text = INVOICE_CREATED.format(amount=plan["amount"], url=existing["pay_url"])
        try:
            await cq.message.edit_text(text, disable_web_page_preview=True)
        except Exception:
            await cq.message.answer(text, disable_web_page_preview=True)
        await cq.answer()
        return

    try:
        invoice = await provider.create_invoice(
            amount_usd=float(plan["amount"]),
            order_id=encode_payload(cq.from_user.id, plan_key),
            description=f"Подписка — {plan['title']}",
        )
    except Exception as e:
        log.exception(
            "create_invoice failed for tg=%s plan=%s provider=%s: %s",
            cq.from_user.id, plan_key, provider_code, e,
        )
        await cq.message.answer(INVOICE_ERROR)
        await cq.answer()
        return

    saved_pay_url = await upsert_pending_payment(
        telegram_id=cq.from_user.id,
        plan=plan_key,
        amount=int(plan["amount"]),
        payment_id=invoice.invoice_id,
        provider=provider_code,
        pay_url=invoice.pay_url,
    )
    if saved_pay_url != invoice.pay_url:
        log.info(
            "reused existing pending invoice (provider returned same payment_id) "
            "tg=%s plan=%s provider=%s payment_id=%s",
            cq.from_user.id, plan_key, provider_code, invoice.invoice_id,
        )

    text = INVOICE_CREATED.format(amount=plan["amount"], url=saved_pay_url)
    try:
        await cq.message.edit_text(text, disable_web_page_preview=True)
    except Exception:
        await cq.message.answer(text, disable_web_page_preview=True)
    await cq.answer()
