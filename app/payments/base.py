from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class Invoice:
    provider: str
    invoice_id: str
    pay_url: str
    amount_usd: float
    raw: Optional[dict] = None


@dataclass
class WebhookEvent:
    provider: str
    invoice_id: str
    status: str
    amount_usd: float
    raw: dict


class PaymentProvider(ABC):
    name: str = ""

    @abstractmethod
    async def create_invoice(
        self,
        amount_usd: float,
        order_id: str,
        description: str = "",
    ) -> Invoice:
        ...

    @abstractmethod
    def verify_webhook(
        self,
        body_bytes: bytes,
        headers: dict,
    ) -> tuple[bool, Optional[WebhookEvent]]:
        """Returns (valid, event). Invalid → (False, None)."""
        ...
