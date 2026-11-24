"""Graph hygiene: drop self-loop edges (src = dst) from `pythia_edge`.

No relation in the registry (references, parent, defines, transposes, internal_reference,
delegates, amends, repeals, supersedes, consolidates, ...) is meaningfully reflexive. A provision
linked to itself carries no navigational signal; it is a PageRank rank trap (a node voting for
itself) and wastes random-walk mass during PPR retrieval.

Older ingestion runs left about 9,000 `cross_reference` rows whose source and target were the same
provision (the extractor matched a provision's own § number in its own text). Materialisation turned
each into an internal_reference edge plus its inverse, about 18,000 self-loops in rel_id 9/10. The
internal-reference resolver (`ingest/resolve_internal.py`) and `graph/materialize.py` now both
reject self-loops at the source, so this script sweeps any residue and is otherwise a no-op.

The operation is reversible: `pythia_edge` is fully derived, and `pythia.graph.materialize` can
rebuild it from `cross_reference` and `provision` at any time. It is also idempotent.

  dry-run :  .venv/bin/python -m pythia.graph.cleanup_self_loops            (default; writes nothing)
  apply   :  .venv/bin/python -m pythia.graph.cleanup_self_loops --apply
"""

from __future__ import annotations

import argparse
import asyncio

from pythia.db import close_pool, get_pool


async def clean_self_loops(*, apply: bool = False) -> dict:
    """Count self-loop edges by type and, when `apply` is set, delete them."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM pythia_edge")
        self_loops = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE src = dst")
        by_type = await conn.fetch(
            "SELECT edge_type, count(*) AS n FROM pythia_edge WHERE src = dst GROUP BY edge_type ORDER BY n DESC"
        )
        removed = 0
        if apply and self_loops:
            tag = await conn.execute("DELETE FROM pythia_edge WHERE src = dst")
            removed = int(tag.split()[-1])  # asyncpg returns the command tag "DELETE <n>"
        after = await conn.fetchval("SELECT count(*) FROM pythia_edge")
    return {
        "before": before,
        "self_loops": self_loops,
        "removed": removed,
        "after": after,
        "by_type": {r["edge_type"]: r["n"] for r in by_type},
    }


def _main() -> None:
    ap = argparse.ArgumentParser(description="Drop self-loop edges (src = dst) from pythia_edge.")
    ap.add_argument(
        "--apply", action="store_true", help="execute the DELETE (default = dry-run preview, writes nothing)"
    )
    a = ap.parse_args()

    async def run():
        s = await clean_self_loops(apply=a.apply)
        tag = "APPLIED" if a.apply else "DRY-RUN (nothing written)"
        print(f"=== self-loop cleanup — {tag} ===")
        print(f"  total edges (before)      : {s['before']:,}")
        print(f"  self-loops (src = dst)    : {s['self_loops']:,}")
        for t, n in s["by_type"].items():
            print(f"      {t:<26} {n:,}")
        if a.apply:
            print(f"  removed                   : {s['removed']:,}")
            print(f"  total edges (after)       : {s['after']:,}")
        else:
            print(
                f"  would remove              : {s['self_loops']:,}  → {s['before'] - s['self_loops']:,} edges remain"
            )
            print("\n  (preview only — re-run with --apply to delete)")
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
