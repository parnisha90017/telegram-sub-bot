"""Unit-тесты для SQL-функций из app.db.queries.

Сейчас покрывают только новые: reduce_subscription, count_legacy_active_users,
is_legacy_active_user. Существующий process_paid_invoice уже покрыт в
test_renewal_extends_granted_access.py."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.queries import (
    ReduceResult,
    count_legacy_active_users,
    is_legacy_active_user,
    reduce_subscription,
)


@asynccontextmanager
async def _acm(value):
    yield value


class FakeConn:
    def __init__(self, fetchrow_results=None, fetch_results=None):
        self._fetchrow_results = list(fetchrow_results or [])
        self._fetch_results = list(fetch_results or [])
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        if not self._fetchrow_results:
            raise AssertionError(f"unexpected extra fetchrow: {sql!r} args={args}")
        result = self._fetchrow_results.pop(0)
        self.executed.append((sql, args))
        return result

    async def fetch(self, sql, *args):
        if not self._fetch_results:
            raise AssertionError(f"unexpected extra fetch: {sql!r} args={args}")
        result = self._fetch_results.pop(0)
        self.executed.append((sql, args))
        return result

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return None

    def transaction(self):
        return _acm(None)


class FakePool:
    def __init__(self, conn=None, fetchval_value=None):
        self._conn = conn
        self.fetchval = AsyncMock(return_value=fetchval_value)

    def acquire(self):
        return _acm(self._conn)


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr("app.db.queries.get_pool", lambda: pool)


# =============================================================================
# reduce_subscription
# =============================================================================

async def test_reduce_found_does_not_go_expired(monkeypatch):
    """Подписка ещё активна после reduce — статус не трогаем, active_granted=[]."""
    new_paid_until = datetime.now(timezone.utc) + timedelta(days=10)
    conn = FakeConn(fetchrow_results=[{"paid_until": new_paid_until}])
    pool = FakePool(conn=conn)
    _patch_pool(monkeypatch, pool)

    result = await reduce_subscription(telegram_id=123, days=3)

    assert isinstance(result, ReduceResult)
    assert result.found is True
    assert result.now_expired is False
    assert result.new_paid_until == new_paid_until
    assert result.active_granted == []

    # 1) UPDATE users RETURNING paid_until → fetchrow
    # 2) UPDATE granted_access (sync) → execute
    # users SET status='expired' и SELECT active_granted НЕ должны выполняться
    sqls = [s for s, _ in conn.executed]
    assert sum("UPDATE users" in s and "status='expired'" in s for s in sqls) == 0
    assert sum("UPDATE granted_access" in s for s in sqls) == 1


async def test_reduce_goes_expired_returns_active_granted(monkeypatch):
    """Подписка ушла в прошлое — статус expired, active_granted с парами
    (chat_id, invite_link) для немедленного кика."""
    new_paid_until = datetime.now(timezone.utc) - timedelta(hours=1)
    conn = FakeConn(
        fetchrow_results=[{"paid_until": new_paid_until}],
        fetch_results=[[
            {"chat_id": -1001, "invite_link": "https://t.me/+aaa"},
            {"chat_id": -1002, "invite_link": "https://t.me/+bbb"},
        ]],
    )
    pool = FakePool(conn=conn)
    _patch_pool(monkeypatch, pool)

    result = await reduce_subscription(telegram_id=42, days=999)

    assert result.found is True
    assert result.now_expired is True
    assert result.active_granted == [
        (-1001, "https://t.me/+aaa"),
        (-1002, "https://t.me/+bbb"),
    ]

    # SQL-проверки: должен быть UPDATE users SET status='expired'
    sqls = [s for s, _ in conn.executed]
    assert any("UPDATE users" in s and "status='expired'" in s for s in sqls)


async def test_reduce_not_found(monkeypatch):
    """Юзера нет (RETURNING вернул None) — found=False, никаких других UPDATE'ов."""
    conn = FakeConn(fetchrow_results=[None])
    pool = FakePool(conn=conn)
    _patch_pool(monkeypatch, pool)

    result = await reduce_subscription(telegram_id=999, days=5)

    assert result.found is False
    assert result.new_paid_until is None
    assert result.now_expired is False
    assert result.active_granted == []
    # Только один SQL-запрос — UPDATE users RETURNING; всё остальное skip.
    assert len(conn.executed) == 1


async def test_reduce_syncs_granted_access_when_active(monkeypatch):
    """SQL UPDATE granted_access выполняется и для не-expired случая
    (синхронизация даты во всех active записях)."""
    new_paid_until = datetime.now(timezone.utc) + timedelta(days=2)
    conn = FakeConn(fetchrow_results=[{"paid_until": new_paid_until}])
    pool = FakePool(conn=conn)
    _patch_pool(monkeypatch, pool)

    await reduce_subscription(telegram_id=7, days=1)

    granted_calls = [(s, a) for s, a in conn.executed if "granted_access" in s]
    assert len(granted_calls) == 1
    sql, args = granted_calls[0]
    assert "UPDATE granted_access" in sql
    assert "SET paid_until = $2" in sql
    assert "revoked_at IS NULL" in sql
    assert args == (7, new_paid_until)


# =============================================================================
# count_legacy_active_users / is_legacy_active_user
# =============================================================================

async def test_count_legacy_active_users(monkeypatch):
    pool = FakePool(fetchval_value=3)
    _patch_pool(monkeypatch, pool)

    result = await count_legacy_active_users()

    assert result == 3
    pool.fetchval.assert_awaited_once()
    sql = pool.fetchval.await_args.args[0]
    # SQL должен искать active юзеров без joined_at записей.
    assert "users" in sql
    assert "active" in sql
    assert "granted_access" in sql
    assert "joined_at IS NOT NULL" in sql


async def test_count_legacy_active_users_zero(monkeypatch):
    pool = FakePool(fetchval_value=0)
    _patch_pool(monkeypatch, pool)
    assert await count_legacy_active_users() == 0


async def test_count_legacy_active_users_handles_none(monkeypatch):
    """COUNT(*) теоретически не возвращает NULL, но защищаемся."""
    pool = FakePool(fetchval_value=None)
    _patch_pool(monkeypatch, pool)
    assert await count_legacy_active_users() == 0


async def test_is_legacy_active_user_true(monkeypatch):
    pool = FakePool(fetchval_value=True)
    _patch_pool(monkeypatch, pool)
    assert await is_legacy_active_user(123) is True
    pool.fetchval.assert_awaited_once()
    sql, *args = pool.fetchval.await_args.args
    assert "EXISTS" in sql
    assert args == [123]


async def test_is_legacy_active_user_false(monkeypatch):
    pool = FakePool(fetchval_value=False)
    _patch_pool(monkeypatch, pool)
    assert await is_legacy_active_user(123) is False
