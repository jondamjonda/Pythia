"""Reading-order edges between consecutive provisions in the same part of an act.

Links each provision to the next one by ordinal that shares its immediate parent path in the
`hierarchy_path` ltree (the path minus its last label), i.e. consecutive provisions inside the same
Díl or Oddíl (division or subdivision), as low-weight `precedes` / `follows` edges. Without them
about a quarter of in-force provisions would be isolated in the graph (see `build`). No self-loops;
idempotent; reversible with --down.

Run (from engine/):
  dry run :  .venv/bin/python -m pythia.ingest.hierarchy_edges
  apply   :  .venv/bin/python -m pythia.ingest.hierarchy_edges --apply
  undo    :  .venv/bin/python -m pythia.ingest.hierarchy_edges --down
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool

# Relations precedes (29) and follows (30) carry weight 0.30: a low-weight navigation backbone, not a
# citation, which the GNN's learned per-relation gate can down-weight further. The edges use their own
# source tag so materialize_layer_a(), which rebuilds only cross_reference, hierarchy, defined_term and
# transposition edges, never deletes them.
_SOURCE = "hierarchy_seq"

# Consecutive provisions (by ordinal) that share the same immediate parent ltree path.
_PAIRS_CTE = """
WITH ranked AS (
    SELECT id, source_document_id,
           subpath(hierarchy_path, 0, nlevel(hierarchy_path) - 1) AS parent_path,
           row_number() OVER (
               PARTITION BY source_document_id, subpath(hierarchy_path, 0, nlevel(hierarchy_path) - 1)
               ORDER BY ordinal, label) AS rn
    FROM provision
    WHERE nlevel(hierarchy_path) > 1
),
pairs AS (
    SELECT a.id AS src, b.id AS dst
    FROM ranked a JOIN ranked b
      ON a.source_document_id = b.source_document_id
     AND a.parent_path = b.parent_path
     AND b.rn = a.rn + 1
     AND a.id <> b.id
)
"""

_COUNT_SQL = _PAIRS_CTE + "SELECT count(*) FROM pairs"
_INSERT_FWD = (
    _PAIRS_CTE
    + """
INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source)
SELECT src, dst, 29, 'precedes', 0.30, '%s' FROM pairs
ON CONFLICT DO NOTHING"""
    % _SOURCE
)
_INSERT_INV = (
    _PAIRS_CTE
    + """
INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source)
SELECT dst, src, 30, 'follows', 0.30, '%s' FROM pairs
ON CONFLICT DO NOTHING"""
    % _SOURCE
)


async def build(*, apply: bool = False) -> dict:
    """Count (and, if `apply`, rewrite) the reading-order edges; report isolated provisions.

    Czech acts are ingested at section (§) level: esbirka.py renders paragraphs (odst.) and letters
    (písm.) inline into one section text and does not set parent_id. The act's real outline (Část,
    Hlava, Díl, Oddíl: part, title, division, subdivision) therefore lives only in the ltree, not in
    `pythia_edge`. Without these edges about a quarter of in-force provisions are isolated in the
    graph, mostly in the large codes such as the Civil Code (89/2012), whose only internal
    citations are paragraph or letter references that resolve to the citing section itself. The
    cross-reference resolvers cannot recover them, but most do have a sibling under the same parent.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        pairs = await conn.fetchval(_COUNT_SQL)
        iso_before = await conn.fetchval(
            """SELECT count(*) FROM provision p WHERE (p.valid_to IS NULL OR p.valid_to > CURRENT_DATE)
               AND NOT EXISTS (SELECT 1 FROM pythia_edge e WHERE e.src = p.id OR e.dst = p.id)"""
        )
        iso_after = iso_before
        edges = 0
        if apply:
            await conn.execute("DELETE FROM pythia_edge WHERE source = $1", _SOURCE)
            await conn.execute(_INSERT_FWD)
            await conn.execute(_INSERT_INV)
            await conn.execute("DELETE FROM pythia_edge WHERE src = dst")
            edges = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE source = $1", _SOURCE)
            iso_after = await conn.fetchval(
                """SELECT count(*) FROM provision p WHERE (p.valid_to IS NULL OR p.valid_to > CURRENT_DATE)
                   AND NOT EXISTS (SELECT 1 FROM pythia_edge e WHERE e.src = p.id OR e.dst = p.id)"""
            )
    return {
        "mode": "APPLIED" if apply else "DRY-RUN",
        "candidate_pairs": pairs,
        "edges_written": edges,
        "isolated_before": iso_before,
        "isolated_after": iso_after,
    }


async def down() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "WITH d AS (DELETE FROM pythia_edge WHERE source = $1 RETURNING 1) SELECT count(*) FROM d", _SOURCE
        )
    return n


async def _main() -> None:
    if "--down" in sys.argv:
        n = await down()
        print(f"⏪ reverted: deleted {n:,} hierarchy_seq edges.")
        await close_pool()
        return
    res = await build(apply=("--apply" in sys.argv))
    print(f"=== hierarchy_seq (precedes/follows) — {res['mode']} ===")
    print(f"  candidate consecutive-sibling pairs : {res['candidate_pairs']:,}")
    print(f"  edges written (×2 directed)         : {res['edges_written']:,}")
    print(f"  isolated in-force BEFORE            : {res['isolated_before']:,}")
    print(f"  isolated in-force AFTER             : {res['isolated_after']:,}")
    if not res["mode"].startswith("APP"):
        print("  (preview only — re-run with --apply to write)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
