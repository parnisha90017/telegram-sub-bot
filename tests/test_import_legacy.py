"""Unit-тесты для import_legacy_user (queries.py).

Проверяем:
  * UPSERT users (создание + продление)
  * granted_access INSERT/UPDATE для каждого chat_id
  * правильные значения source / invite_link / joined_at
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.db.queries import ImportLegacyResult, import_legacy_user


@asynccontextmanager
async def _acm(value):
    yield value


class FakeConn:
    """Программируемая FakeConn: fetchrow_results — список значений (по
    очереди); execute_results — список (str | callable). callable получает
    sql и возвращает str вроде "UPDATE 0" / "INSERT 0 1".
    Все вызовы записываются в self.calls (sql, args)."""

    def __init__(self, fetchrow_results=None, execute_results=None):
        self._fetchrow_results = list(fetchrow_results or [])
        self._execute_results = list(execute_results or [])
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if not self._fetchrow_results:
            raise AssertionError(f"unexpected fetchrow {sql!r}")
        return self._fetchrow_results.pop(0)

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if self._execute_results:
            r = self._execute_results.pop(0)
            return r(sql) if callable(r) else r
        # default — INSERT 0 1
        return "INSERT 0 1"

    def transaction(self):
        return _acm(None)


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _acm(self._conn)


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr("app.db.queries.get_pool", lambda: pool)


CHAT_IDS = [-1001, -1002, -1003, -1004]


async def test_import_legacy_creates_new_user_and_grants(monkeypatch):
    new_paid_until = datetime(2026, 6, 1, tzinfo=timezone.utc)
    conn = FakeConn(
        fetchrow_results=[
            None,                                  # SELECT 1 FROM users → нет
            {"paid_until": new_paid_until},        # INSERT users RETURNING
        ],
        # 4 итерации чата: каждая — UPDATE granted_access (0 affected) +
        # INSERT granted_access. Чередуются.
        execute_results=[
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
        ],
    )
    pool = FakePool(conn)
    _patch_pool(monkeypatch, pool)

    result = await import_legacy_user(123, 30, CHAT_IDS)

    assert isinstance(result, ImportLegacyResult)
    assert result.was_created is True
    assert result.granted_count == 4
    assert result.new_paid_until == new_paid_until

    # Был INSERT users
    sqls = [s for s, _ in conn.calls]
    assert any("INSERT INTO users" in s for s in sqls)
    # Было 4 INSERT granted_access с source='legacy_import'
    legacy_inserts = [
        (s, a) for s, a in conn.calls
        if "INSERT INTO granted_access" in s and "legacy_import" in s
    ]
    assert len(legacy_inserts) == 4
    # invite_link стоит NULL
    for sql, _args in legacy_inserts:
        assert "NULL" in sql
        assert "joined_at" in sql


async def test_import_legacy_existing_user_extends_subscription(monkeypatch):
    new_paid_until = datetime(2026, 6, 15, tzinfo=timezone.utc)
    conn = FakeConn(
        fetchrow_results=[
            {"telegram_id": 123},                  # SELECT — существует
            {"paid_until": new_paid_until},        # UPDATE users RETURNING
        ],
        execute_results=[
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
        ],
    )
    pool = FakePool(conn)
    _patch_pool(monkeypatch, pool)

    result = await import_legacy_user(123, 7, CHAT_IDS)

    assert result.was_created is False
    assert result.granted_count == 4
    sqls = [s for s, _ in conn.calls]
    # был UPDATE users (с GREATEST), не INSERT
    assert any("UPDATE users" in s and "GREATEST" in s for s in sqls)
    assert not any("INSERT INTO users" in s for s in sqls)


async def test_import_legacy_updates_existing_granted_access(monkeypatch):
    """Если у юзера уже есть active granted_access для chat_id — UPDATE
    вместо INSERT (granted_count всё равно 4: каждый chat учтён)."""
    new_paid_until = datetime(2026, 6, 15, tzinfo=timezone.utc)
    conn = FakeConn(
        fetchrow_results=[
            {"telegram_id": 123},                  # users exists
            {"paid_until": new_paid_until},        # UPDATE users RETURNING
        ],
        # Все 4 UPDATE затронули по 1 строке — INSERT не нужен
        execute_results=[
            "UPDATE 1",
            "UPDATE 1",
            "UPDATE 1",
            "UPDATE 1",
        ],
    )
    pool = FakePool(conn)
    _patch_pool(monkeypatch, pool)

    result = await import_legacy_user(123, 30, CHAT_IDS)

    assert result.granted_count == 4
    insert_calls = [s for s, _ in conn.calls if "INSERT INTO granted_access" in s]
    assert insert_calls == [], (
        f"INSERT не должен выполняться когда UPDATE затронул строку, got {insert_calls}"
    )


async def test_import_legacy_passes_correct_paid_until_to_granted_access(monkeypatch):
    new_paid_until = datetime(2026, 6, 15, tzinfo=timezone.utc)
    conn = FakeConn(
        fetchrow_results=[None, {"paid_until": new_paid_until}],
        execute_results=[
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
            "UPDATE 0", "INSERT 0 1",
        ],
    )
    pool = FakePool(conn)
    _patch_pool(monkeypatch, pool)

    await import_legacy_user(42, 30, CHAT_IDS)

    inserts = [
        a for s, a in conn.calls
        if "INSERT INTO granted_access" in s and "legacy_import" in s
    ]
    for args in inserts:
        # args = (telegram_id, chat_id, paid_until)
        assert args[0] == 42
        assert args[1] in CHAT_IDS
        assert args[2] == new_paid_until


async def test_import_legacy_empty_chat_ids(monkeypatch):
    """Дегенеративный кейс: chat_ids=[] → granted_count=0, юзер всё равно
    создан/продлён."""
    new_paid_until = datetime(2026, 6, 15, tzinfo=timezone.utc)
    conn = FakeConn(
        fetchrow_results=[None, {"paid_until": new_paid_until}],
        execute_results=[],
    )
    pool = FakePool(conn)
    _patch_pool(monkeypatch, pool)

    result = await import_legacy_user(99, 30, [])

    assert result.was_created is True
    assert result.granted_count == 0
