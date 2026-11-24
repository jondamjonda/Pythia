"""Provision embeddings with BAAI/bge-m3, computed locally (MPS when available, no API key).

Fills `provision.embedding_bge` (pgvector, 1024-d) with bge-m3, a multilingual dense retrieval
model (8192-token context, covers Czech) that gives seed retrieval and the GNN one shared vector
space. It supersedes `embedding_local` (768-d paraphrase-multilingual-mpnet, kept as a fallback).
bge-m3 dense embeddings are symmetric, so queries need no prefix. Optional staleness tracking
(config.temporal_substrate_enabled, default off) is described above `_ensure_stale_column`.

Run (from engine/):
      .venv/bin/python -m pythia.ingest.embed_bge                  # embed provisions missing a vector
      .venv/bin/python -m pythia.ingest.embed_bge --all            # (re-)embed everything
      .venv/bin/python -m pythia.ingest.embed_bge --reembed-stale  # re-embed only stale vectors
                                                                   # (text changed since last embed)
"""

from __future__ import annotations

import asyncio
import sys

from pythia.config import get_settings
from pythia.db import close_pool, get_pool
from pythia.ingest._resource_signals import (
    FreeDiskExhausted,
    KillSwitchEngaged,
    check_between_batches,
    write_mps_heartbeat,
)

EMB_MODEL = "BAAI/bge-m3"  # 1024-d dense, multilingual (incl. cs), 8192 ctx
DIM = 1024
# MPS-safe cap on the characters embedded per provision. Raising it to 12000 pushed bge-m3 to
# ~8192-token attention and ran MPS out of memory (a 14 GiB buffer) mid-run. Truncating very long
# sections of the general codes and the reasoning (odůvodnění) of rulings is a real but minor
# retrieval gap; the memory-safe fix is chunking long texts (embed sub-windows, then max-pool), not a
# bigger single window.
MAX_CHARS = 4000
_model = None


_device = "cpu"


def model():
    """Return the shared bge-m3 model, loading it on first use (also used by seeding and reranking)."""
    global _model, _device
    if _model is None:
        import torch
        from sentence_transformers import SentenceTransformer

        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        _model = SentenceTransformer(EMB_MODEL, device=dev)
        _model.max_seq_length = 8192
        _device = dev
    return _model


def _vec(e) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in e) + "]"


async def ensure_column() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"ALTER TABLE provision ADD COLUMN IF NOT EXISTS embedding_bge vector({DIM})")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS provision_embedding_bge_idx ON provision "
            "USING hnsw (embedding_bge vector_cosine_ops)"
        )
        # Staleness column, created only when temporal_substrate_enabled is on (otherwise the column
        # does not exist and the embed UPDATE never references it). `embedded_text_hash` records the
        # text_hash that was fed to the encoder, written in the same UPDATE as the vector, so the two
        # always change together. Invariant:
        #     embedded_text_hash == text_hash  <=>  the vector embeds the current text
        #     embedded_text_hash <> text_hash  <=>  the vector is stale (text changed since embedding)
        # This matters for an amendment path that updates provision text in place and so leaves the
        # vector non-null. The bulk ingest path deletes and reinserts provisions with fresh ids and a
        # NULL vector, so there it is defence in depth. Additive and idempotent. The partial index's
        # predicate matches stale_vector_ids exactly (embedding_bge IS NOT NULL and a differing hash),
        # so a never-embedded row counts as absent, not stale.
        if get_settings().temporal_substrate_enabled:
            await conn.execute("ALTER TABLE provision ADD COLUMN IF NOT EXISTS embedded_text_hash text")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_provision_stale_vector ON provision (id) "
                "WHERE embedding_bge IS NOT NULL AND embedded_text_hash IS DISTINCT FROM text_hash"
            )


async def embed_all(
    *, only_missing: bool = True, batch: int = 256, enc_batch: int = 16, ids: list[str] | None = None
) -> int:
    """Fill provision.embedding_bge with bge-m3 and return the number of provisions embedded.

    Args:
        only_missing: if True, embed only provisions with a NULL vector (new ingests). If False,
            re-embed the whole eligible set; never do that corpus-wide by accident after an
            amendment. `reembed_stale` passes False together with `ids` to confine the work.
        batch: provisions per database round trip (and per kill-switch / disk check).
        enc_batch: batch size passed to the encoder.
        ids: if given, restrict the eligible set to exactly these provision ids.

    With temporal_substrate_enabled off (the default) the SELECT does not read text_hash and the
    UPDATE writes only the vector, so it works on a database without the staleness column. With it
    on, the same UPDATE also stamps embedded_text_hash with the row's text_hash.
    """
    stamp = get_settings().temporal_substrate_enabled
    await ensure_column()
    pool = await get_pool()
    # Structural anchor rows have an empty body and only link the parent_id chain; they are never
    # retrievable, so keep them out of the dense index.
    where = "text IS NOT NULL AND NOT is_anchor_only" + (" AND embedding_bge IS NULL" if only_missing else "")
    params_in: list = []
    if ids is not None:
        where += " AND id = ANY($1::uuid[])"  # scoped re-embed, never the whole corpus
        params_in = [ids]
    # With staleness tracking on, also read text_hash for the stamp.
    cols = "id::text id, left(text, {n}) txt" + (", text_hash" if stamp else "")
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT {cols.format(n=MAX_CHARS)} FROM provision WHERE {where}", *params_in)
    if not rows:
        print("nothing to embed (all provisions already have a current embedding_bge)")
        return 0
    print(f"embedding {len(rows):,} provisions with {EMB_MODEL} (dim={DIM}) …")
    m = model()
    done = 0
    for i in range(0, len(rows), batch):
        # Poll the cross-process control signals (shared data/ directory): abort cleanly if the kill
        # switch is engaged or free disk fell below the floor. The exception propagates to the caller,
        # which reports the partial count. Checked before encoding so a doomed batch never starts.
        check_between_batches()
        chunk = rows[i : i + batch]
        embs = m.encode(
            [r["txt"] for r in chunk], normalize_embeddings=True, batch_size=enc_batch, show_progress_bar=False
        )
        # With staleness tracking on, write the vector and stamp embedded_text_hash from the same row's
        # text_hash in one statement, so the invariant cannot drift. Otherwise write only the vector.
        if stamp:
            params = [(r["id"], _vec(e), r["text_hash"]) for r, e in zip(chunk, embs)]
            async with pool.acquire() as conn:
                await conn.executemany(
                    "UPDATE provision SET embedding_bge = $2::vector, embedded_text_hash = $3 WHERE id = $1::uuid",
                    params,
                )
        else:
            params = [(r["id"], _vec(e)) for r, e in zip(chunk, embs)]
            async with pool.acquire() as conn:
                await conn.executemany("UPDATE provision SET embedding_bge = $2::vector WHERE id = $1::uuid", params)
        done += len(chunk)
        # Refresh the MPS heartbeat each batch so the app sees the live allocation (a heartbeat older
        # than 30 s means no torch process is running).
        write_mps_heartbeat(_device)
        print(f"  {done:,}/{len(rows):,}", flush=True)
    return done


# --------------------------------------------------------------------------- staleness tracking
# With config.temporal_substrate_enabled on, each embed also stamps `embedded_text_hash` with the
# text_hash it encoded, so a vector whose provision text has since changed can be found and
# re-embedded. The helpers below and the `--reembed-stale` CLI work regardless of the flag, since
# they are how it is activated; only the automatic refresh and scheduler entry points are gated.
# To activate, deliberately and with a snapshot to fall back on:
#   1. take a fresh database snapshot (pg_dump);
#   2. set PYTHIA_TEMPORAL_SUBSTRATE_ENABLED=1;
#   3. run `-m pythia.ingest.embed_bge --reembed-stale` once (creates embedded_text_hash and
#      reconciles);
#   4. run recompute_provision_temporal_status(apply=True) once (fills pit_status);
#   5. confirm the d1_check, n5_check and n6_check integrity checks pass.
async def _ensure_stale_column() -> None:
    """Create the staleness column and its partial index if missing (idempotent).

    Used by the staleness helpers below, which are the explicit operator path (and are exercised by
    tests), so they create the column regardless of the flag. ensure_column(), by contrast, only
    creates it when the flag is on, so the automatic embed path leaves the schema alone.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE provision ADD COLUMN IF NOT EXISTS embedded_text_hash text")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_provision_stale_vector ON provision (id) "
            "WHERE embedding_bge IS NOT NULL AND embedded_text_hash IS DISTINCT FROM text_hash"
        )


async def stale_vector_ids(*, ids: list[str] | None = None) -> list[str]:
    """Return ids of provisions whose vector is stale (embedded_text_hash differs from text_hash).

    A stale vector was computed for text that has since changed in place. The result is optionally
    intersected with `ids`. Reads only, apart from creating the staleness column on demand; not
    gated by the flag.

    A provision that was never embedded also has embedded_text_hash NULL, which is "distinct from"
    text_hash. Those belong to the only_missing=True path, so this also requires embedding_bge IS NOT
    NULL: the stale set is exactly "has a vector, but for old text". A missing vector is absent, not
    stale.
    """
    await _ensure_stale_column()
    pool = await get_pool()
    where = "embedding_bge IS NOT NULL AND NOT is_anchor_only AND embedded_text_hash IS DISTINCT FROM text_hash"
    args: list = []
    if ids is not None:
        where += " AND id = ANY($1::uuid[])"
        args = [ids]
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"SELECT id::text id FROM provision WHERE {where}", *args)
    return [r["id"] for r in rows]


async def reembed_stale(*, ids: list[str] | None = None) -> int:
    """Re-embed only the provisions whose vector is stale; never a corpus-wide re-embed.

    An in-place text update leaves embedding_bge non-null, so the only_missing=True pass would skip
    the row and its vector would silently describe the old text. This finds exactly the rows whose
    text_hash changed and re-embeds them (only_missing=False, confined to those ids); afterwards
    embedded_text_hash == text_hash again. The bulk amendment path in esbirka deletes and reinserts
    provisions with fresh ids and a NULL vector, so there this finds nothing.

    `ids` optionally narrows the scope further (e.g. to the provisions an amendment touched); the
    result is the intersection of the stale set and `ids`. Returns the number re-embedded (0 when
    nothing is stale).
    """
    stale = await stale_vector_ids(ids=ids)
    if not stale:
        print("no stale vectors — nothing to re-embed (embedded_text_hash == text_hash everywhere in scope)")
        return 0
    print(f"scoped re-embed: {len(stale):,} stale provision(s) (text changed since last embed) …")
    return await embed_all(only_missing=False, ids=stale)  # only_missing=False, but scoped to the stale ids


async def _main() -> None:
    if "--reembed-stale" in sys.argv:  # reconcile stale vectors only
        n = await reembed_stale()
        print(f"done · re-embedded {n:,} stale provisions")
        await close_pool()
        return
    only_missing = "--all" not in sys.argv
    try:
        n = await embed_all(only_missing=only_missing)
        print(f"done · embedded {n:,} provisions")
    except (KillSwitchEngaged, FreeDiskExhausted) as exc:
        # A control signal aborted the run between batches. Embeddings written so far are committed
        # (each batch is its own transaction). Exit non-zero so a wrapping script sees the halt, with a
        # one-line message instead of a traceback.
        print(f"ABORTED · {exc}", flush=True)
        await close_pool()
        sys.exit(2)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
