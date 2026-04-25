"""process_paid_invoice должен внутри той же транзакции продлять paid_until
во всех активных granted_access записях для этого telegram_id. Это закрывает
кейс продления для юзера, который уже сидит в каналах с прошлой подписки."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.queries import process_paid_invoice


@asynccontextmanager
async def _acm(value):
    yield value


class FakeConn:
    def __init__(self, fetchrow_results):
        self._fetchrow_results = list(fetchrow_results)
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        if not self._fetchrow_results:
            raise AssertionError(f"unexpected extra fetchrow: {sql!r} args={args}")
        result = self._fetchrow_results.pop(0)
        self.executed.append((sql, args))
        return result

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return None

    def transaction(self):
        return _acm(None)


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _acm(self._conn)


def _new_paid_until() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=10)


async def test_paid_again_extends_existing_granted_access(monkeypatch):
    """Юзер 555 платит tariff_7d. process_paid_invoice внутри транзакции
    должен сделать UPDATE granted_access SET paid_until=<new> для активных
    записей этого юзера."""
    new_paid_until = _new_paid_until()

    fetchrow_results = [
        # 1) SELECT FROM payments FOR UPDATE
        {
            "telegram_id": 555,
            "plan": "tariff_7d",
            "amount": Decimal("21"),
            "status": "pending",
        },
        # 2) UPDATE users RETURNING paid_until
        {"paid_until": new_paid_until},
    ]
    conn = FakeConn(fetchrow_results)
    pool = FakePool(conn)
    monkeypatch.setattr("app.db.queries.get_pool", lambda: pool)

    result = await process_paid_invoice(
        provider="heleket",
        payment_id="hel-uuid-xyz",
        webhook_amount=Decimal("21"),
    )

    assert result == {
        "telegram_id": 555,
        "plan": "tariff_7d",
        "paid_until": new_paid_until,
    }

    granted_calls = [(s, a) for s, a in conn.executed if "granted_access" in s]
    assert len(granted_calls) == 1, (
        f"expected exactly one UPDATE granted_access, got {granted_calls}"
    )
    sql, args = granted_calls[0]
    assert "UPDATE granted_access" in sql
    assert "paid_until = $2" in sql
    assert "joined_at IS NOT NULL" in sql
    assert "revoked_at IS NULL" in sql
    assert args == (555, new_paid_until)


async def test_unknown_invoice_does_not_touch_granted_access(monkeypatch):
    """Неизвестный invoice → return None до UPDATE users → UPDATE
    granted_access НЕ должен исполняться."""
    fetchrow_results = [None]  # SELECT FROM payments → не найдено
    conn = FakeConn(fetchrow_results)
    pool = FakePool(conn)
    monkeypatch.setattr("app.db.queries.get_pool", lambda: pool)

    result = await process_paid_invoice(
        provider="heleket", payment_id="missing", webhook_amount=Decimal("11"),
    )

    assert result is None
    granted_calls = [(s, a) for s, a in conn.executed if "granted_access" in s]
    assert granted_calls == []


async def test_already_paid_invoice_does_not_touch_granted_access(monkeypatch):
    """Идемпотентность: если status уже 'paid' → return None до UPDATE users
    → granted_access не трогаем (двойного продления не происходит)."""
    fetchrow_results = [
        {
            "telegram_id": 555,
            "plan": "tariff_7d",
            "amount": Decimal("21"),
            "status": "paid",  # ← уже обработан
        },
    ]
    conn = FakeConn(fetchrow_results)
    pool = FakePool(conn)
    monkeypatch.setattr("app.db.queries.get_pool", lambda: pool)

    result = await process_paid_invoice(
        provider="heleket", payment_id="hel-uuid-xyz", webhook_amount=Decimal("21"),
    )

    assert result is None
    granted_calls = [(s, a) for s, a in conn.executed if "granted_access" in s]
    assert granted_calls == []


async def test_amount_mismatch_does_not_touch_granted_access(monkeypatch):
    fetchrow_results = [
        {
            "telegram_id": 555,
            "plan": "tariff_7d",
            "amount": Decimal("21"),  # ожидали 21
            "status": "pending",
        },
    ]
    conn = FakeConn(fetchrow_results)
    pool = FakePool(conn)
    monkeypatch.setattr("app.db.queries.get_pool", lambda: pool)

    # webhook прислал 1.00 — не совпало
    result = await process_paid_invoice(
        provider="heleket", payment_id="hel-uuid-xyz", webhook_amount=Decimal("1.00"),
    )

    assert result is None
    granted_calls = [(s, a) for s, a in conn.executed if "granted_access" in s]
    assert granted_calls == []
