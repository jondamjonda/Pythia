"""Switch provision full-text search to the Czech-stemming text-search config.

Rebuilds the generated `provision.tsv` column with `to_tsvector('czech', text)` (hunspell stemming,
so banka/banky/bance/bankou all index as the lemma `banka`) and recreates its GIN index. Fully
reversible: `tsv` is derived from `text`, so `--simple` switches back to the unstemmed config.
Requires the `czech` text-search config (unaccent plus the czech_hunspell ispell dictionary; see
scripts/setup_czech_fts.sh).

Run (from engine/):
      .venv/bin/python -m pythia.ingest.czech_fts            # use the 'czech' config
      .venv/bin/python -m pythia.ingest.czech_fts --simple   # revert to 'simple'
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool


async def switch(config: str = "czech") -> None:
    assert config in ("czech", "simple")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("ALTER TABLE provision DROP COLUMN IF EXISTS tsv")
            await conn.execute(
                f"ALTER TABLE provision ADD COLUMN tsv tsvector "
                f"GENERATED ALWAYS AS (to_tsvector('{config}', text)) STORED"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS ix_p_tsv ON provision USING gin (tsv)")
        n = await conn.fetchval("SELECT count(*) FROM provision WHERE tsv IS NOT NULL")
        print(f"provision.tsv rebuilt with '{config}' config (+ GIN index) · {n:,} rows")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(switch("simple" if "--simple" in sys.argv else "czech"))
