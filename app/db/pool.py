from __future__ import annotations

from pathlib import Path

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=1,
            max_size=10,
            command_timeout=10,
        )
        await _apply_migrations()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool is not initialized. Call init_pool() first.")
    return _pool


async def _apply_migrations() -> None:
    sql = (Path(__file__).parent / "migrations.sql").read_text(encoding="utf-8")
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(sql)
