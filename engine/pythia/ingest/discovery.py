"""Publication-feed watcher: detect newly published or changed acts and queue them for triage.

Reads a publication feed through the sbirkapp listing code and records candidates in its own table,
`discovery_queue`. It never ingests, reindexes or touches any other table: it detects, and leaves
acting on the candidates to later stages. It is off unless `discovery_enabled` is set, dry-runs by
default, and is additive and idempotent. `parse_feed` is the pure, unit-tested core.

Run (from engine/): .venv/bin/python -m pythia.ingest.discovery  (always a dry run)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

import httpx

from pythia.config import get_settings
from pythia.ingest.sbirkapp import _UA, _TIMEOUT, ISSUERS, _parse_cz_date, list_issuer

_log = logging.getLogger("pythia.ingest.discovery")

# Source tag naming the feed a candidate came from. It is stored in discovery_queue.source and is part
# of the dedup key, so the same external_id seen on two feeds gives two distinct rows.
_SOURCE_SBIRKAPP = "sbirkapp"


# ── Queue table (created on first enabled use, never at import) ──────────
# Follows the inline-DDL convention of clause_index.ensure_clause_table and refresh.py: raw asyncpg DDL,
# IF NOT EXISTS, a uuid_generate_v4() default and jsonb metadata. It holds newly seen or changed acts
# awaiting triage; this module only writes it. Inserting into it is the watcher's only write and no
# existing row is ever mutated, so dropping the table is a complete rollback.
DISCOVERY_DDL = """
CREATE TABLE IF NOT EXISTS discovery_queue (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_id text NOT NULL,
    published_on date,
    source text,
    status text NOT NULL DEFAULT 'pending',
    seen_at timestamptz NOT NULL DEFAULT now(),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (external_id, source)
)
"""


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A newly seen or changed act spotted on a publication feed, not yet ingested.

    Attributes:
        external_id: The act's stable identifier in its source registry (for sbirkapp, the SPP id).
        published_on: The publication or effect date parsed from the feed row, or None if it could
            not be parsed. The watcher never invents a date.
        source: The feed the candidate came from (part of the discovery_queue dedup key).
        title: A human-readable label for triage, or None when the feed has none.
    """

    external_id: str
    published_on: date | None
    source: str
    title: str | None = None


async def ensure_queue(conn) -> None:
    """Create discovery_queue if absent (idempotent DDL).

    Called only from the enabled persist path, never at import or startup.
    """
    await conn.execute(DISCOVERY_DDL)


def parse_feed(payload, *, source: str = _SOURCE_SBIRKAPP) -> list[DiscoveryCandidate]:
    """Turn an already-fetched feed payload into de-duplicated candidates (pure; no network or DB).

    The payload is what the sbirkapp listing returns: an iterable of row dicts with at least an `spp`
    key (the external id) plus the Czech listing cells (`ucinnost`, the effect date; `nazev` or
    `druh`, the title). Rows without an external id are skipped. The effect date is parsed with
    sbirkapp's Czech-date helper and becomes None when unparseable. Duplicate external ids within one
    payload collapse to the first occurrence; the listing is newest first, so that is the freshest.
    """
    out: list[DiscoveryCandidate] = []
    seen: set[str] = set()
    for row in payload or []:
        ext = (row.get("spp") or "").strip()
        if not ext or ext in seen:
            continue
        seen.add(ext)
        published_on = _parse_cz_date(row.get("ucinnost"))
        title = (row.get("nazev") or row.get("druh") or "").strip() or None
        out.append(DiscoveryCandidate(external_id=ext, published_on=published_on, source=source, title=title))
    return out


async def _fetch_sbirkapp_feed(*, cap: int) -> list[dict]:
    """Fetch the newest-first listing rows for every watched issuer via sbirkapp.list_issuer.

    Returns the merged row payload that parse_feed consumes. Network only; no database.
    """
    rows: list[dict] = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        for issuer in ISSUERS:
            try:
                rows.extend(await list_issuer(client, issuer, cap=cap))
            except Exception as exc:  # noqa: BLE001 — one issuer's listing failing must not abort the watch
                _log.warning("discovery: list %s failed: %r", issuer.name, exc)
    return rows


async def discover(*, since: date | None = None, dry_run: bool = True, cap: int = 50) -> list[DiscoveryCandidate]:
    """Watch the publication feed for newly published or changed acts (detection only, no ingestion).

    Returns [] immediately unless `discovery_enabled` is set. Otherwise fetches the feed, parses it
    and keeps only candidates published strictly after the `since` watermark (None keeps all; a
    candidate with an unknown date is kept, since it cannot be shown to be old). Only when `dry_run`
    is false are the candidates inserted as status 'pending'; the INSERT is ON CONFLICT (external_id,
    source) DO NOTHING, so watching the same feed again never duplicates a row.

    Returns the filtered candidates in either case.
    """
    if not get_settings().discovery_enabled:
        return []

    payload = await _fetch_sbirkapp_feed(cap=cap)
    candidates = parse_feed(payload, source=_SOURCE_SBIRKAPP)
    if since is not None:
        # Keep candidates newer than the watermark. Unknown-date candidates are kept: they cannot be
        # shown to predate the watermark, and dropping them could silently lose a new act.
        candidates = [c for c in candidates if c.published_on is None or c.published_on > since]

    if dry_run:
        return candidates

    if candidates:
        from pythia.db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            await ensure_queue(conn)
            await conn.executemany(
                """
                INSERT INTO discovery_queue (external_id, published_on, source, status)
                VALUES ($1, $2, $3, 'pending')
                ON CONFLICT (external_id, source) DO NOTHING
                """,
                [(c.external_id, c.published_on, c.source) for c in candidates],
            )
    return candidates


# ── Per-publisher discovery for the auto-ingest scheduler ──────────────
# The CLI above always dry-runs. The scheduler's `discover` job calls discover_publishers() to persist
# pending rows. It iterates the configured publisher watch-list, so new publishers (EU, ČNB) can be
# added without touching the cycle code, but only publishers with a wired feed fetch anything; the
# rest are reported as skipped rather than producing candidates. Each candidate carries its feed
# `source` (the dedup key), and each feed is bounded by `cap` (the discover_cap setting).

# Configured publisher name to the discovery feed that covers it. Only sbirkapp (the listing of
# regional and municipal regulations used by discover()) is wired.
_PUBLISHER_FEEDS: dict[str, str] = {
    "Sbírka zákonů": _SOURCE_SBIRKAPP,
}


def _configured_publishers() -> list[str]:
    raw = getattr(get_settings(), "auto_ingest_publishers", "") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


async def discover_publishers(*, since: date | None = None, cap: int = 50) -> dict:
    """Persist pending discovery rows for every configured publisher with a wired feed.

    This is the scheduler's discover job. Returns {"enabled": False, ...} and writes nothing unless
    `discovery_enabled` is set. When enabled, runs `discover(dry_run=False)` (an idempotent persist)
    for each publisher in `auto_ingest_publishers` that has an entry in `_PUBLISHER_FEEDS`, and
    counts candidates per publisher; publishers without a wired feed are listed under `skipped`.
    Raises only what discover() raises; a single issuer's fetch failure is already logged and
    swallowed in _fetch_sbirkapp_feed.
    """
    if not get_settings().discovery_enabled:
        return {"enabled": False, "persisted": 0, "by_publisher": {}, "skipped": []}
    by_pub: dict[str, int] = {}
    skipped: list[str] = []
    total = 0
    for pub in _configured_publishers():
        feed = _PUBLISHER_FEEDS.get(pub)
        if feed is None:
            skipped.append(pub)  # no wired feed (e.g. EU, ČNB): no rows
            _log.info("discovery: publisher %r has no wired feed yet — skipped (deferred)", pub)
            continue
        cands = await discover(since=since, dry_run=False, cap=cap)  # persists; idempotent via ON CONFLICT
        by_pub[pub] = len(cands)
        total += len(cands)
    return {"enabled": True, "persisted": total, "by_publisher": by_pub, "skipped": skipped}


async def _main() -> None:
    # Convenience CLI: always a dry run (discovery_enabled still decides whether anything is fetched).
    cands = await discover(dry_run=True)
    print(
        f"discovery {'ENABLED' if get_settings().discovery_enabled else 'OFF'} — {len(cands)} candidate(s)", flush=True
    )
    for c in cands[:20]:
        print(f"  {c.source:10s} {c.external_id:20s} {c.published_on} · {(c.title or '')[:50]}", flush=True)


if __name__ == "__main__":
    asyncio.run(_main())
