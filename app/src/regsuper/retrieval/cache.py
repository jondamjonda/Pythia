"""TTL-bounded JSON cache backed by the `retrieval_cache` table, plus an optional in-process LRU.

HyDE and query rewriting are treated as deterministic for a (query, jurisdiction, sector)
triple within a 12-hour window. Caching them removes the dominant cost and latency of the
retrieval pipeline: the two LLM legs account for over 70% of the per-query cost budget.

Cache keys are sha256 over `(kind, normalised_query, jurisdiction, sector)`. Normalisation
collapses whitespace and lowercases, which absorbs trivial typing variations without false
positives across genuinely different queries.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from regsuper.storage.database import session_scope
from regsuper.storage.models import RetrievalCache

DEFAULT_TTL = timedelta(hours=12)
_WHITESPACE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Bounded in-process embedding cache
# ---------------------------------------------------------------------------
# The persistent embedding cache lives in Postgres (`retrieval_cache`), so it never grows the
# Python heap. This optional, process-local LRU sits in front of it as a hot-path accelerator
# for a long-running server, capped so it can never grow without bound.
#
# Off by default: the cap comes from REGSUPER_EMBED_INPROC_CACHE_MAX, or from the caller's
# `default_max` (0 unless the caller opts in) when the variable is unset. With a cap <= 0 the
# cache is inert: `get` and `put` are no-ops, so consumers behave exactly like the DB-only path.
# With a positive cap N it holds at most N entries and evicts the least recently used.
# Embeddings are deterministic for a fixed (text, model, dim) key, so an LRU hit returns exactly
# what a DB hit would; only the working-set memory is bounded.
#
# _DEFAULT_INPROC_CACHE_MAX is a generous cap for callers that opt in without their own number
# (via get_embed_inproc_cache(default_max=...)): 50,000 entries. At 1536 float dimensions that
# is well under typical server headroom while covering a large warm set.
_DEFAULT_INPROC_CACHE_MAX = 50_000
_ENV_INPROC_CACHE_MAX = "REGSUPER_EMBED_INPROC_CACHE_MAX"


class BoundedLRUCache:
    """A minimal, thread-safe, size-capped LRU map.

    Used as the optional in-process layer for embeddings. ``maxsize <= 0`` disables the
    cache (every get misses and every put is dropped), which is the default unless an
    operator opts in. The cap is a hard ceiling on entry count, so memory cannot grow
    without bound.
    """

    def __init__(self, maxsize: int) -> None:
        self.maxsize = int(maxsize)
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.maxsize > 0

    def get(self, key: str) -> Any | None:
        if self.maxsize <= 0:
            return None
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)  # mark most-recently-used
            return self._data[key]

    def put(self, key: str, value: Any) -> None:
        if self.maxsize <= 0:
            return
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = value
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


def _resolve_inproc_cache_max(default_max: int) -> int:
    """Return the cap from REGSUPER_EMBED_INPROC_CACHE_MAX if set, else ``default_max``.

    An explicit 0 forces the cache off. An unparseable value also turns it off (fail safe).
    """
    raw = os.environ.get(_ENV_INPROC_CACHE_MAX)
    if raw is None or raw.strip() == "":
        return int(default_max)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


_INPROC_EMBED_CACHE: BoundedLRUCache | None = None
_INPROC_LOCK = threading.Lock()


def get_embed_inproc_cache(*, default_max: int = 0) -> BoundedLRUCache:
    """Return the process-wide bounded in-process embedding cache (a singleton).

    ``default_max`` is the cap used only when the env var is unset: pass a positive number
    (e.g. ``_DEFAULT_INPROC_CACHE_MAX``) to opt a caller in by default, or leave it 0 so the
    cache stays inert unless the operator sets the env var. The first call wins; later calls
    return the same instance, so the cap is stable for the lifetime of the process.
    """
    global _INPROC_EMBED_CACHE
    if _INPROC_EMBED_CACHE is None:
        with _INPROC_LOCK:
            if _INPROC_EMBED_CACHE is None:
                _INPROC_EMBED_CACHE = BoundedLRUCache(_resolve_inproc_cache_max(default_max))
    return _INPROC_EMBED_CACHE


def reset_embed_inproc_cache() -> None:
    """Drop the singleton so the next get_embed_inproc_cache() re-reads the env.

    For tests; not used on the live path.
    """
    global _INPROC_EMBED_CACHE
    with _INPROC_LOCK:
        _INPROC_EMBED_CACHE = None


def _normalise(s: str | None) -> str:
    if not s:
        return ""
    return _WHITESPACE.sub(" ", s.strip()).lower()


def make_key(kind: str, query: str, *, jurisdiction: str = "", sector: str = "") -> str:
    """Return a stable cache key; `kind` separates HyDE, rewrite, embedding and other entries."""
    payload = "|".join([kind, _normalise(query), _normalise(jurisdiction), _normalise(sector)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def get_cached(key: str) -> Any | None:
    """Return the cached JSON value, or None if it is missing or expired.

    Expired rows are not deleted here; that is left to a periodic cleanup task.
    """
    async with session_scope() as session:
        row = (
            await session.execute(
                sql_text("SELECT value, expires_at FROM retrieval_cache WHERE cache_key = :k"),
                {"k": key},
            )
        ).first()
    if row is None:
        return None
    value, expires_at = row
    if expires_at is None or expires_at < datetime.now(UTC):
        return None
    return value


async def put_cached(
    key: str,
    *,
    kind: str,
    value: Any,
    ttl: timedelta = DEFAULT_TTL,
) -> None:
    """Upsert `value` under `key`, expiring after `ttl`.

    The JSONB column accepts dict, list, str, int, bool or None.
    """
    now = datetime.now(UTC)
    expires = now + ttl
    stmt = pg_insert(RetrievalCache).values(
        cache_key=key,
        kind=kind,
        value=value,
        created_at=now,
        expires_at=expires,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[RetrievalCache.cache_key],
        set_={
            "kind": kind,
            "value": value,
            "created_at": now,
            "expires_at": expires,
        },
    )
    async with session_scope() as session:
        await session.execute(stmt)
