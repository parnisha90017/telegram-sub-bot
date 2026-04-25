from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Optional

import aiohttp

from app.payments.base import Invoice, PaymentProvider, WebhookEvent

logger = logging.getLogger(__name__)

HELEKET_API_URL = "https://api.heleket.com/v1/payment"

# Heleket payment lifecycle statuses. "paid_over" is an overpayment — merchant
# still keeps the funds, so we normalize it to "paid" for the downstream
# webhook handler (which only reacts to status == "paid").
PAID_STATUSES = {"paid", "paid_over"}


def _canonical_json(body: dict) -> bytes:
    """Serialize a dict exactly the way PHP's json_encode does for Heleket's
    MD5 signature. Compact separators, UTF-8, and backslash-escape forward
    slashes — PHP does this by default, Python's json.dumps does not."""
    s = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("/", "\\/")
    return s.encode("utf-8")


def _sign(body: dict, api_key: str) -> str:
    body_b64 = base64.b64encode(_canonical_json(body)).decode("ascii")
    return hashlib.md5((body_b64 + api_key).encode("utf-8")).hexdigest()


class HeleketProvider(PaymentProvider):
    name = "heleket"

    def __init__(
        self,
        merchant_uuid: str,
        api_key: str,
        callback_url: str,
        api_url: str = HELEKET_API_URL,
    ):
        self.merchant_uuid = merchant_uuid
        self.api_key = api_key
        self.callback_url = callback_url
        self.api_url = api_url

    async def create_invoice(
        self,
        amount_usd: float,
        order_id: str,
        description: str = "",
    ) -> Invoice:
        body = {
            "amount": f"{amount_usd:.2f}",
            "currency": "USDT",
            "network": "tron",
            "order_id": str(order_id),
            "url_callback": self.callback_url,
            "lifetime": 3600,
        }
        # Serialize ONCE, then hash-and-send the same bytes. Using json=body
        # lets aiohttp re-serialize with its own separators/escape rules, which
        # produces a different byte stream than what we hashed → Heleket
        # computes MD5 of the bytes it received and gets a different digest
        # → "Invalid Sign". This was the prod bug.
        body_bytes = _canonical_json(body)
        body_b64 = base64.b64encode(body_bytes).decode("ascii")
        sign = hashlib.md5((body_b64 + self.api_key).encode("utf-8")).hexdigest()

        headers = {
            "merchant": self.merchant_uuid,
            "sign": sign,
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.api_url,
                data=body_bytes,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.error(
                "Heleket createInvoice non-JSON response. "
                "body=%r sign=%s response=%r",
                body_bytes, sign, text[:500],
            )
            raise RuntimeError(f"Heleket createInvoice non-JSON response: {text[:200]}")

        if data.get("state") != 0:
            logger.error(
                "Heleket createInvoice failed. body=%r sign=%s response=%r",
                body_bytes, sign, data,
            )
            raise RuntimeError(f"Heleket createInvoice failed: {data}")

        result = data.get("result") or {}
        uuid = result.get("uuid")
        pay_url = result.get("url")
        if not uuid or not pay_url:
            raise RuntimeError(f"Heleket createInvoice malformed response: {data}")

        return Invoice(
            provider=self.name,
            invoice_id=str(uuid),
            pay_url=str(pay_url),
            amount_usd=amount_usd,
            raw=result,
        )

    def verify_webhook(
        self,
        body_bytes: bytes,
        headers: dict,
    ) -> tuple[bool, Optional[WebhookEvent]]:
        """Heleket sends the MD5 signature inside the JSON body as `sign`.
        The `headers` arg is ignored (present only to match PaymentProvider)."""
        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Heleket webhook body is not valid JSON")
            return False, None

        if not isinstance(payload, dict):
            return False, None

        received_sign = payload.pop("sign", None)
        if not received_sign:
            logger.warning("Heleket webhook missing sign field")
            return False, None

        expected_sign = _sign(payload, self.api_key)
        if not hmac.compare_digest(expected_sign, str(received_sign)):
            logger.warning("Heleket webhook signature mismatch")
            return False, None

        # Restore sign so the raw dict has the original payload
        payload["sign"] = received_sign

        raw_status = str(payload.get("status", "")).lower()
        normalized = "paid" if raw_status in PAID_STATUSES else raw_status

        try:
            amount_usd = float(payload.get("amount", 0))
        except (TypeError, ValueError):
            amount_usd = 0.0

        return True, WebhookEvent(
            provider=self.name,
            invoice_id=str(payload.get("uuid", "")),
            status=normalized,
            amount_usd=amount_usd,
            raw=payload,
        )
