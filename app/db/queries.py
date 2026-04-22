from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.db.pool import get_pool


async def upsert_user(telegram_id: int, username: str | None) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO users (telegram_id, username)
        VALUES ($1, $2)
        ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username
        """,
        telegram_id, username,
    )


async def get_user_by_tg_id(telegram_id: int) -> dict[str, Any] | None:
    pool = get_pool()
    row = await pool.fetchrow(
        "SELECT telegram_id, username, plan, paid_until, status FROM users WHERE telegram_id = $1",
        telegram_id,
    )
    return dict(row) if row else None


async def insert_pending_payment(
    telegram_id: int,
    plan: str,
    amount: Decimal | int | float,
    payment_id: str,
) -> None:
    pool = get_pool()
    await pool.execute(
        """
        INSERT INTO payments (telegram_id, plan, amount, payment_id, status)
        VALUES ($1, $2, $3, $4, 'pending')
        """,
        telegram_id, plan, Decimal(str(amount)), payment_id,
    )


PLAN_DAYS: dict[str, int] = {"tariff_3d": 3, "tariff_7d": 7, "tariff_30d": 30}


async def process_paid_invoice(
    invoice_id: str,
    webhook_amount: Decimal,
) -> int | None:
    """Атомарно помечает платёж paid и продлевает подписку.

    Возвращает telegram_id для выдачи invite-ссылок, или None если:
      - платёж неизвестен,
      - уже обработан (идемпотентность),
      - amount не совпадает.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT telegram_id, plan, amount, status
                  FROM payments
                 WHERE payment_id = $1
                 FOR UPDATE
                """,
                invoice_id,
            )
            if row is None:
                return None
            if row["status"] == "paid":
                return None
            if webhook_amount != row["amount"]:
                return None

            days = PLAN_DAYS[row["plan"]]
            await conn.execute(
                "UPDATE payments SET status = 'paid' WHERE payment_id = $1",
                invoice_id,
            )
            await conn.execute(
                """
                UPDATE users
                   SET plan = $2,
                       paid_until = GREATEST(COALESCE(paid_until, NOW()), NOW())
                                  + make_interval(days => $3),
                       status = 'active'
                 WHERE telegram_id = $1
                """,
                row["telegram_id"], row["plan"], days,
            )
            return row["telegram_id"]


async def list_expiring_between(start: datetime, end: datetime) -> list[int]:
    pool = get_pool()
    rows = await pool.fetch(
        """
        SELECT telegram_id
          FROM users
         WHERE status = 'active'
           AND paid_until BETWEEN $1 AND $2
        """,
        start, end,
    )
    return [r["telegram_id"] for r in rows]


async def expire_and_return_ids() -> list[int]:
    pool = get_pool()
    now = datetime.now(timezone.utc)
    rows = await pool.fetch(
        """
        UPDATE users
           SET status = 'expired'
         WHERE status = 'active' AND paid_until < $1
        RETURNING telegram_id
        """,
        now,
    )
    return [r["telegram_id"] for r in rows]
