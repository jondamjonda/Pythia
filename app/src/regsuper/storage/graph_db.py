"""Read-only engine for the Pythia corpus database (the legal knowledge graph).

This is a separate Postgres database from the app database in ``regsuper.storage.database``:
the read-only corpus behind the interactive act-graph view (``regsuper.api.graph``). Because it
is a different database, the org row-level-security settings are not issued here (they apply
to the app engine only). Every query through this engine is SELECT-only, so there is nothing to
commit.

The engine is created lazily, and only when ``settings.pythia_database_url`` is configured;
otherwise ``get_graph_engine()`` returns ``None`` and callers must degrade gracefully (the
graph endpoints answer ``{"enabled": false, ...}``). The module globals are kept separate from
the app engine so disposing one never touches the other.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from regsuper.config import get_settings

_graph_engine: AsyncEngine | None = None
_graph_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_graph_engine() -> AsyncEngine | None:
    """Lazily build the read-only corpus engine, or ``None`` if unconfigured.

    Returns ``None`` rather than raising when ``settings.pythia_database_url`` is unset, so the
    graph endpoints can answer ``enabled: false`` instead of failing with a 500. The pool is
    small (5 connections, 10 overflow) because this engine serves occasional graph requests,
    not the hot product path.
    """
    global _graph_engine
    url = get_settings().pythia_database_url
    if not url:
        return None
    if _graph_engine is None:
        _graph_engine = create_async_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            future=True,
        )
    return _graph_engine


def _get_graph_session_factory() -> async_sessionmaker[AsyncSession] | None:
    global _graph_session_factory
    engine = get_graph_engine()
    if engine is None:
        return None
    if _graph_session_factory is None:
        _graph_session_factory = async_sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False
        )
    return _graph_session_factory


@asynccontextmanager
async def graph_session_scope() -> AsyncIterator[AsyncSession]:
    """Yield an :class:`AsyncSession` on the read-only corpus engine.

    Raises ``RuntimeError`` if the corpus database is not configured; callers should check
    ``get_graph_engine() is not None`` first and degrade gracefully. No session settings are
    issued and nothing is committed (the corpus is read-only).
    """
    factory = _get_graph_session_factory()
    if factory is None:
        raise RuntimeError("pythia_database_url not configured; corpus engine unavailable")
    async with factory() as session:
        yield session


async def dispose_graph_engine() -> None:
    """Dispose the corpus engine (the counterpart of ``dispose_engine``).

    Not wired into the app lifespan; exposed for tests and manual teardown so the corpus pool
    can be released without touching the app engine.
    """
    global _graph_engine, _graph_session_factory
    if _graph_engine is not None:
        await _graph_engine.dispose()
    _graph_engine = None
    _graph_session_factory = None
