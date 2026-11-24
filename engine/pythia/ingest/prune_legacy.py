"""Drop legacy tables and columns inherited from an earlier schema, then reclaim the space.

Removes data nothing in the engine reads any more (DROP_TABLES and DROP_COLUMNS), then runs
VACUUM FULL on `provision` to reclaim the column space and rebuild its indexes. Destructive, so it
only runs with `--yes`.

Run (from engine/): .venv/bin/python -m pythia.ingest.prune_legacy --yes
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool

# The dropped objects: `provision_sentence` (an old sentence-level OpenAI index over the first few
# documents), the unused application tables api_call, audit_event and finding, `embedding_semantic`
# (1536-d OpenAI vectors, superseded by bge-m3), and `embedding_domain` and `embedding_resolved`
# (always empty). The GNN uses relation embeddings and the labeling trick, not provision content
# embeddings, so it is unaffected. `embedding_local` (mpnet) is kept as the fallback embedding.
DROP_TABLES = ["provision_sentence", "api_call", "audit_event", "finding"]
DROP_COLUMNS = ["embedding_semantic", "embedding_domain", "embedding_resolved"]


async def prune() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        for t in DROP_TABLES:
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
            print(f"dropped table {t}")
        cols = ", ".join(f"DROP COLUMN IF EXISTS {c}" for c in DROP_COLUMNS)
        await conn.execute(f"ALTER TABLE provision {cols}")
        print(f"dropped provision columns: {', '.join(DROP_COLUMNS)}")
        print("VACUUM FULL provision (reclaiming space + rebuilding indexes) …")
        await conn.execute("VACUUM (FULL, ANALYZE) provision")
        after = await conn.fetchval("SELECT pg_size_pretty(pg_database_size(current_database()))")
        print(f"\nDB size: {before} → {after}")
    await close_pool()


if __name__ == "__main__":
    if "--yes" not in sys.argv:
        print("destructive — re-run with --yes to execute")
    else:
        asyncio.run(prune())
