from __future__ import annotations

from typing import Optional

from aiocryptopay import AioCryptoPay

from app.payments.base import Invoice, PaymentProvider, WebhookEvent


class CryptoPayProvider(PaymentProvider):
    """PaymentProvider-compatible facade over `aiocryptopay.AioCryptoPay`.

    Webhook verification is NOT implemented — CryptoBot updates are still
    routed through the library's own dispatcher (`crypto.get_updates` +
    `@crypto.pay_handler()`) which does HMAC-SHA256 internally. This wrapper
    only unifies invoice creation under PaymentProvider so the bot UI can
    treat all providers uniformly.
    """

    name = "cryptobot"

    def __init__(self, client: AioCryptoPay, bot_username: str = ""):
        self._client = client
        self._bot_username = bot_username

    async def create_invoice(
        self,
        amount_usd: float,
        order_id: str,
        description: str = "",
    ) -> Invoice:
        kwargs: dict = {
            "asset": "USDT",
            "amount": float(amount_usd),
            "payload": str(order_id),
            "expires_in": 3600,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        if description:
            kwargs["description"] = description
        if self._bot_username:
            kwargs["paid_btn_name"] = "callback"
            kwargs["paid_btn_url"] = f"https://t.me/{self._bot_username}"

        inv = await self._client.create_invoice(**kwargs)

        try:
            raw = inv.model_dump()
        except Exception:
            try:
                raw = inv.dict()
            except Exception:
                raw = None

        return Invoice(
            provider=self.name,
            invoice_id=str(inv.invoice_id),
            pay_url=inv.bot_invoice_url,
            amount_usd=float(amount_usd),
            raw=raw,
        )

    def verify_webhook(
        self,
        body_bytes: bytes,
        headers: dict,
    ) -> tuple[bool, Optional[WebhookEvent]]:
        raise NotImplementedError(
            "CryptoPayProvider.verify_webhook is not used. "
            "CryptoBot webhooks are handled by aiocryptopay.get_updates "
            "(see app/main.py route /cryptopay/webhook + "
            "app/payments/webhook.py register_cryptopay_handlers)."
        )
