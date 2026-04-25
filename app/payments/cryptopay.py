from __future__ import annotations

from aiocryptopay import AioCryptoPay, Networks

from app.bot.keyboards import PLANS
from app.config import settings


def build_client() -> AioCryptoPay:
    network = Networks.MAIN_NET if settings.crypto_pay_network == "main" else Networks.TEST_NET
    return AioCryptoPay(token=settings.crypto_pay_token, network=network)


def encode_payload(telegram_id: int, plan_key: str) -> str:
    """Используется как order_id для платёжного провайдера и payload в CryptoBot.
    Heleket валидирует order_id по alpha_dash (`[A-Za-z0-9_-]`), поэтому
    разделитель — `_`, а не `:`."""
    return f"{telegram_id}_{plan_key}"


def decode_payload(payload: str) -> tuple[int, str]:
    """Парсит '<tg>_<plan>' (новый формат) или '<tg>:<plan>' (legacy, до фикса
    alpha_dash). plan_key сам содержит `_` (например 'tariff_3d'), поэтому
    при выборе разделителя сначала проверяем `:` (legacy маркер): он встречается
    только как разделитель, тогда как `_` входит и в plan_key."""
    sep = ":" if ":" in payload else "_"
    tg_id_str, plan_key = payload.split(sep, 1)
    return int(tg_id_str), plan_key


async def create_invoice_for(
    crypto: AioCryptoPay,
    telegram_id: int,
    plan_key: str,
) -> tuple[int, str]:
    plan = PLANS[plan_key]
    invoice = await crypto.create_invoice(
        asset="USDT",
        amount=float(plan["amount"]),
        description=f"Подписка — {plan['title']}",
        payload=encode_payload(telegram_id, plan_key),
        expires_in=3600,
        paid_btn_name="callback",
        paid_btn_url=f"https://t.me/{settings.bot_username}",
        allow_comments=False,
        allow_anonymous=False,
    )
    return invoice.invoice_id, invoice.bot_invoice_url
