from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import PLANS, provider_pick_kb
from app.bot.texts import INVOICE_CREATED, INVOICE_ERROR, SELECT_PROVIDER
from app.config import Settings
from app.db.queries import (
    find_active_pending_payment,
    find_expired_pending_payment,
    mark_pending_refreshed,
    upsert_pending_payment,
    upsert_user,
)
from app.payments.base import PaymentProvider
from app.payments.cryptopay import encode_payload

log = logging.getLogger(__name__)
router = Router()

# Heleket-специфика: для истёкших pending'ов (старше TTL=1ч) не плодим новый
# uuid, а вызываем create_invoice(is_refresh=True) — он обновит address +
# expired_at, uuid и pay_url остаются те же. Параметр поддерживается ТОЛЬКО
# Heleket-API; CryptoBot про него не знает и сам генерит уникальный
# invoice_id каждый раз — гейтим refresh-ветку по provider_code.
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

    # Шаг 1: переиспользуем активный pending этого юзера+тарифа+провайдера,
    # если он моложе часа. Это закрывает кейс «юзер ткнул второй раз»: в Heleket
    # не идём, отдаём тот же pay_url. Записи без pay_url (legacy до миграции)
    # find игнорирует — для них пойдём в провайдер и через upsert проставим URL.
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

    # Шаг 2 (только Heleket): если есть истёкший pending (>1ч) — refresh
    # вместо нового createInvoice. Heleket для того же order_id вернёт ТОТ ЖЕ
    # uuid с обновлёнными address/expired_at — так юзер получит рабочий QR
    # без второй записи в payments. CryptoBot про is_refresh не знает и сам
    # генерит уникальные invoice_id, ему refresh не нужен — пропускаем.
    if provider_code == HELEKET:
        expired = await find_expired_pending_payment(
            telegram_id=cq.from_user.id,
            plan=plan_key,
            provider=provider_code,
        )
        if expired is not None:
            try:
                refreshed = await provider.create_invoice(
                    amount_usd=float(plan["amount"]),
                    order_id=encode_payload(cq.from_user.id, plan_key),
                    description=f"Подписка — {plan['title']}",
                    is_refresh=True,
                )
            except Exception as e:
                log.exception(
                    "heleket refresh failed for tg=%s plan=%s payment_id=%s: %s",
                    cq.from_user.id, plan_key, expired["payment_id"], e,
                )
                await cq.message.answer(INVOICE_ERROR)
                await cq.answer()
                return

            # По доке Heleket "Изменены только address, payment_status и
            # expired_at" — uuid не меняется. Защита: если когда-нибудь API
            # начнёт возвращать новый uuid при refresh, увидим в логах и
            # поправим стратегию. Использование expired["payment_id"] (а не
            # refreshed.invoice_id) гарантирует, что UPDATE найдёт именно
            # ту запись, для которой мы делали refresh — иначе при разъезде
            # uuid'ов в БД останется мёртвая ссылка.
            if refreshed.invoice_id != expired["payment_id"]:
                log.warning(
                    "heleket refresh returned different uuid: old=%s new=%s "
                    "tg=%s plan=%s",
                    expired["payment_id"], refreshed.invoice_id,
                    cq.from_user.id, plan_key,
                )

            await mark_pending_refreshed(
                provider=provider_code,
                payment_id=expired["payment_id"],
                pay_url=refreshed.pay_url,
            )
            log.info(
                "refreshed expired heleket invoice tg=%s plan=%s payment_id=%s",
                cq.from_user.id, plan_key, expired["payment_id"],
            )

            text = INVOICE_CREATED.format(amount=plan["amount"], url=refreshed.pay_url)
            try:
                await cq.message.edit_text(text, disable_web_page_preview=True)
            except Exception:
                await cq.message.answer(text, disable_web_page_preview=True)
            await cq.answer()
            return

    # Шаг 3: создаём новый инвойс у провайдера.
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

    # Шаг 4: UPSERT. ON CONFLICT (provider, payment_id) DO UPDATE — закрывает
    # race и кейс «Heleket вернул тот же uuid»: вторая вставка не падает,
    # pay_url пишется в существующую запись и возвращается RETURNING.
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
