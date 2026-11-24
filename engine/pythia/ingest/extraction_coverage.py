"""Manifest of which acts each practical-fact extractor wave has actually run over.

It lets `fact_lookup` tell `extraction_not_run` (the act was never swept: a silent fallback, no gap
event) from a genuine `no_fact_row` (the act was swept but this provision has no fact), so an
unswept act no longer wrongly reports "no fact here". The table is additive and optional: the writer
and readers degrade to no-ops when it is absent, and creating it and backfilling coverage on the
live corpus is a separate operator step.
"""

from __future__ import annotations

import contextlib

# One row per (act, wave, extractor_version): the fact that the wave ran over the act, plus scanned
# and written counts for observability. The UNIQUE key backs the ON CONFLICT upsert, so re-running
# the same wave over the same act refreshes the counts in place instead of adding a row.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS extraction_coverage (
  id                  uuid        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  external_id         text        NOT NULL,          -- the act (source_document.external_id) the wave ran over
  wave                text        NOT NULL,           -- 'regex' | 'llm'
  extractor_version   text        NOT NULL,           -- provenance (e.g. 'regex-b3p1-v1' / 'llm-lhut-v1')
  provisions_scanned  integer     NOT NULL DEFAULT 0, -- provisions of this act the wave scanned
  facts_written       integer     NOT NULL DEFAULT 0, -- facts the wave wrote for this act (observability)
  ran_at              timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_ec_act_wave_ver UNIQUE (external_id, wave, extractor_version)
);
CREATE INDEX IF NOT EXISTS ix_ec_external_id ON extraction_coverage (external_id);
"""

_UPSERT_SQL = """
INSERT INTO extraction_coverage
    (external_id, wave, extractor_version, provisions_scanned, facts_written, ran_at)
VALUES ($1, $2, $3, $4, $5, now())
ON CONFLICT (external_id, wave, extractor_version) DO UPDATE
   SET provisions_scanned = EXCLUDED.provisions_scanned,
       facts_written      = EXCLUDED.facts_written,
       ran_at             = now()
"""


async def _table_present(conn) -> bool:
    """Return True if the extraction_coverage table exists.

    When it does not, the writer and readers degrade to no-ops (the same guard as for the
    provision_clause and practical_fact tables).
    """
    return bool(await conn.fetchval("SELECT to_regclass('public.extraction_coverage') IS NOT NULL"))


async def ensure_table(conn) -> None:
    """Create the coverage table and its index if missing (idempotent).

    Called only by an explicit schema apply, as an alternative to the sibling .sql file. The writer
    never calls it: a wave run against a database without the schema must stay a no-op rather than
    silently create the table.
    """
    await conn.execute(CREATE_TABLE_SQL)


async def record_wave(
    conn, external_id: str, wave: str, extractor_version: str, *, provisions_scanned: int = 0, facts_written: int = 0
) -> bool:
    """Record that `wave` at `extractor_version` ran over the act `external_id`.

    Called by the enrich_practical wave runner. Idempotent upsert on (external_id, wave,
    extractor_version) that also stores the scanned and written counts. Returns True if a row was
    written. If the table does not exist, returns False without creating it or raising. An empty
    external_id is skipped, since a coverage row must name an act.
    """
    if not (external_id or "").strip():
        return False
    if not await _table_present(conn):
        return False  # table absent: no-op, never create it implicitly
    await conn.execute(
        _UPSERT_SQL, external_id.strip(), wave, extractor_version, int(provisions_scanned), int(facts_written)
    )
    return True


async def covered_acts(conn, external_ids: list[str] | None = None) -> set[str]:
    """Return the act external_ids in the coverage manifest, optionally limited to `external_ids`.

    Coverage is per act, not per wave: an act appears as soon as any wave has recorded it. Returns an
    empty set when the table does not exist, so every act reads as not covered and fact_lookup reports
    extraction_not_run, the honest state before a backfill.
    """
    async with _absent_safe(conn) as present:
        if not present:
            return set()
        if external_ids:
            rows = await conn.fetch(
                "SELECT DISTINCT external_id FROM extraction_coverage WHERE external_id = ANY($1::text[])", external_ids
            )
        else:
            rows = await conn.fetch("SELECT DISTINCT external_id FROM extraction_coverage")
    return {r["external_id"] for r in rows}


async def coverage_counts(conn) -> dict:
    """Return the coverage block for nexus_stats.

    Includes acts covered, a breakdown by wave and extractor version, and total scanned and written
    counts. Returns a zeroed block with present=False when the table does not exist.
    """
    async with _absent_safe(conn) as present:
        if not present:
            return {
                "present": False,
                "acts_covered": 0,
                "rows": 0,
                "provisions_scanned": 0,
                "facts_written": 0,
                "by_wave": [],
            }
        acts = await conn.fetchval("SELECT count(DISTINCT external_id) FROM extraction_coverage")
        rows = await conn.fetchval("SELECT count(*) FROM extraction_coverage")
        agg = await conn.fetchrow(
            "SELECT COALESCE(sum(provisions_scanned),0) AS scanned, "
            "COALESCE(sum(facts_written),0) AS written FROM extraction_coverage"
        )
        by_wave = await conn.fetch(
            "SELECT wave, extractor_version, count(DISTINCT external_id) AS acts, "
            "sum(facts_written) AS facts FROM extraction_coverage GROUP BY wave, extractor_version "
            "ORDER BY wave, extractor_version"
        )
    return {
        "present": True,
        "acts_covered": int(acts or 0),
        "rows": int(rows or 0),
        "provisions_scanned": int(agg["scanned"] or 0),
        "facts_written": int(agg["written"] or 0),
        "by_wave": [
            {
                "wave": r["wave"],
                "extractor_version": r["extractor_version"],
                "acts": int(r["acts"] or 0),
                "facts": int(r["facts"] or 0),
            }
            for r in by_wave
        ],
    }


@contextlib.asynccontextmanager
async def _absent_safe(conn):
    """Yield whether the coverage table exists; keeps the readers' absent-table guard in one place."""
    yield await _table_present(conn)
