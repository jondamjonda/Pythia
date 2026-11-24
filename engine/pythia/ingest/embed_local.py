"""Local provision embeddings for dense (semantic) seeding, with no API key needed.

Fills `provision.embedding_local` (pgvector) using a local multilingual sentence model, so the
seeder can match a query to provisions by meaning and across languages (a Czech question can hit
an English EU provision), not only by shared keywords.

Run (from engine/):
      .venv/bin/python -m pythia.ingest.embed_local            # embed provisions missing a vector
      .venv/bin/python -m pythia.ingest.embed_local --all      # re-embed everything
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool

EMB_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"  # 768-d, multilingual (incl. cs)
DIM = 768
_model = None


def model():
    global _model
    if _model is None:
        import torch
        from sentence_transformers import SentenceTransformer

        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = SentenceTransformer(EMB_MODEL, device=dev)
    return _model


def _vec(e) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


async def ensure_column() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"ALTER TABLE provision ADD COLUMN IF NOT EXISTS embedding_local vector({DIM})")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS provision_embedding_local_idx ON provision "
            "USING hnsw (embedding_local vector_cosine_ops)"
        )


async def embed_all(*, only_missing: bool = True, batch: int = 256) -> int:
    await ensure_column()
    pool = await get_pool()
    where = "text IS NOT NULL" + (" AND embedding_local IS NULL" if only_missing else "")
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT id::text id, left(text, 2000) txt FROM provision WHERE {where}")
    if not rows:
        print("nothing to embed (all provisions already have embedding_local)")
        return 0
    print(f"embedding {len(rows):,} provisions with {EMB_MODEL} …")
    m = model()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        embs = m.encode([r["txt"] for r in chunk], normalize_embeddings=True, batch_size=64, show_progress_bar=False)
        params = [(r["id"], _vec(e)) for r, e in zip(chunk, embs)]
        async with pool.acquire() as conn:
            await conn.executemany("UPDATE provision SET embedding_local = $2::vector WHERE id = $1::uuid", params)
        done += len(chunk)
        print(f"  {done:,}/{len(rows):,}")
    return done


async def _main() -> None:
    only_missing = "--all" not in sys.argv
    n = await embed_all(only_missing=only_missing)
    print(f"done · embedded {n:,} provisions")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
