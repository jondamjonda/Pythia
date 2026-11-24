"""Async database access: a shared asyncpg connection pool over the legal-graph Postgres.

asyncpg is used directly rather than through an ORM because the graph work is array-heavy raw SQL
(recursive CTEs over uuid[] paths), where asyncpg's typed parameter binding is the cleanest fit.
"""

from __future__ import annotations

import asyncpg

from pythia.config import get_settings

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    # Strip the SQLAlchemy driver suffix to get a plain libpq DSN for asyncpg.
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # Pool size comes from PYTHIA_DB_POOL_MIN_SIZE / PYTHIA_DB_POOL_MAX_SIZE (default 1 and 5).
        # It affects resource footprint only, never query results. min_size is floored at 1 so a
        # stray zero or negative override cannot produce an empty pool that breaks acquire().
        s = get_settings()
        min_size = max(1, int(s.db_pool_min_size))
        max_size = max(min_size, int(s.db_pool_max_size))
        _pool = await asyncpg.create_pool(_dsn(), min_size=min_size, max_size=max_size)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
