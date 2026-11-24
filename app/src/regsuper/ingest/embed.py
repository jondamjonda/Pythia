"""Embed provisions and sentences with OpenAI and build the HNSW indexes.

Uses text-embedding-3-large reduced from 3072 to 1536 dimensions to fit the VECTOR(1536)
columns provision.embedding_semantic and provision_sentence.embedding. Embeddings are also
written to data/intermediate/embeddings/*.npz so a failed apply can be replayed without
paying OpenAI again. Requests are synchronous rather than via the Batch API: the corpus is
about 3M tokens (roughly $0.40), and the tracked_embedding wrapper keeps cost logging,
kill-switch and spend-limit enforcement without a 24-hour batch wait.

Usage:
    python -m regsuper.ingest.embed --all
    python -m regsuper.ingest.embed --provisions
    python -m regsuper.ingest.embed --sentences
    python -m regsuper.ingest.embed --indexes
    python -m regsuper.ingest.embed --sanity
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
from openai import AsyncOpenAI
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_embedding
from regsuper.observability.events import emit
from regsuper.observability.logging_config import configure_logging
from regsuper.storage.database import get_engine, session_scope
from regsuper.storage.models import Provision, ProvisionSentence

EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 1536

# OpenAI accepts up to 2048 items and about 300k tokens per request. The average sentence
# is about 50 tokens; batches are also capped by characters to stay well below both limits
# even when a single provision is long.
_BATCH_MAX_ITEMS = 256
_BATCH_MAX_CHARS = 200_000


def _snapshot_dir() -> Path:
    p = get_settings().data_dir / "intermediate" / "embeddings"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Loading work
# ---------------------------------------------------------------------------
async def _load_pending_provisions() -> list[tuple[UUID, str]]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Provision.id, Provision.text)
                .where(Provision.embedding_semantic.is_(None))
                .where(Provision.text.is_not(None))
                .order_by(Provision.source_document_id, Provision.ordinal)
            )
        ).all()
    return [(r[0], r[1]) for r in rows if r[1] and r[1].strip()]


async def _load_pending_sentences() -> list[tuple[UUID, str]]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ProvisionSentence.id, ProvisionSentence.text)
                .where(ProvisionSentence.embedding.is_(None))
                .where(ProvisionSentence.text.is_not(None))
                .order_by(ProvisionSentence.provision_id, ProvisionSentence.sentence_index)
            )
        ).all()
    return [(r[0], r[1]) for r in rows if r[1] and r[1].strip()]


# ---------------------------------------------------------------------------
# Batching and embedding
# ---------------------------------------------------------------------------
def _chunk_by_budget(
    items: list[tuple[UUID, str]],
) -> list[list[tuple[UUID, str]]]:
    """Pack (id, text) pairs into batches that respect the item and character limits."""
    out: list[list[tuple[UUID, str]]] = []
    cur: list[tuple[UUID, str]] = []
    cur_chars = 0
    for it in items:
        char_len = len(it[1])
        if cur and (len(cur) >= _BATCH_MAX_ITEMS or cur_chars + char_len > _BATCH_MAX_CHARS):
            out.append(cur)
            cur = []
            cur_chars = 0
        cur.append(it)
        cur_chars += char_len
    if cur:
        out.append(cur)
    return out


async def _embed_one_batch(
    client: AsyncOpenAI,
    *,
    source: str,
    batch: list[tuple[UUID, str]],
) -> list[tuple[UUID, list[float]]]:
    response = await tracked_embedding(
        client,
        source=source,
        model=EMBED_MODEL,
        input=[t for _, t in batch],
        dimensions=EMBED_DIM,
    )
    data = response.data
    if len(data) != len(batch):
        raise RuntimeError(f"embedding count mismatch: requested {len(batch)} got {len(data)}")
    return [(batch[i][0], list(data[i].embedding)) for i in range(len(batch))]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def _apply_provision_embeddings(rows: list[tuple[UUID, list[float]]]) -> int:
    if not rows:
        return 0
    stmt = text("UPDATE provision SET embedding_semantic = :emb WHERE id = :id").bindparams(
        bindparam("id", type_=PG_UUID(as_uuid=True))
    )
    async with session_scope() as session:
        for rid, vec in rows:
            await session.execute(stmt, {"id": rid, "emb": str(vec)})
    return len(rows)


async def _apply_sentence_embeddings(rows: list[tuple[UUID, list[float]]]) -> int:
    if not rows:
        return 0
    stmt = text("UPDATE provision_sentence SET embedding = :emb WHERE id = :id").bindparams(
        bindparam("id", type_=PG_UUID(as_uuid=True))
    )
    async with session_scope() as session:
        for rid, vec in rows:
            await session.execute(stmt, {"id": rid, "emb": str(vec)})
    return len(rows)


def _write_snapshot(
    kind: str,
    rows: list[tuple[UUID, list[float]]],
) -> Path:
    """Persist embeddings to a compressed .npz file in the data directory.

    The file holds `ids` (string array) and `vecs` (float32, shape [N, EMBED_DIM]). Snapshots
    are kept for 30 days so a failed apply can be replayed without re-billing OpenAI.
    """
    if not rows:
        return _snapshot_dir() / f"{kind}_empty.npz"
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    p = _snapshot_dir() / f"{kind}_{ts}.npz"
    ids = np.array([str(r[0]) for r in rows], dtype=object)
    vecs = np.array([r[1] for r in rows], dtype=np.float32)
    np.savez_compressed(p, ids=ids, vecs=vecs)
    return p


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def embed_provisions(*, progress_every: int = 1000) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    pending = await _load_pending_provisions()
    total = len(pending)
    await emit("embed.provisions.start", total=total, model=EMBED_MODEL, dim=EMBED_DIM)
    if total == 0:
        await emit("embed.provisions.complete", embedded=0, total=0)
        return {"embedded": 0, "total": 0}

    batches = _chunk_by_budget(pending)
    all_rows: list[tuple[UUID, list[float]]] = []
    done = 0
    t0 = time.perf_counter()

    for batch in batches:
        rows = await _embed_one_batch(client, source="embed.provisions", batch=batch)
        await _apply_provision_embeddings(rows)
        all_rows.extend(rows)
        done += len(rows)
        if done % progress_every < len(rows):
            await emit("embed.provisions.progress", done=done, total=total)

    snap = _write_snapshot("provisions", all_rows)
    elapsed = time.perf_counter() - t0
    await emit(
        "embed.provisions.complete",
        embedded=done,
        total=total,
        snapshot=str(snap),
        seconds=round(elapsed, 1),
    )
    return {"embedded": done, "total": total, "snapshot": str(snap), "seconds": elapsed}


async def embed_sentences(*, progress_every: int = 2000) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    pending = await _load_pending_sentences()
    total = len(pending)
    await emit("embed.sentences.start", total=total, model=EMBED_MODEL, dim=EMBED_DIM)
    if total == 0:
        await emit("embed.sentences.complete", embedded=0, total=0)
        return {"embedded": 0, "total": 0}

    batches = _chunk_by_budget(pending)
    all_rows: list[tuple[UUID, list[float]]] = []
    done = 0
    t0 = time.perf_counter()

    for batch in batches:
        rows = await _embed_one_batch(client, source="embed.sentences", batch=batch)
        await _apply_sentence_embeddings(rows)
        all_rows.extend(rows)
        done += len(rows)
        if done % progress_every < len(rows):
            await emit("embed.sentences.progress", done=done, total=total)

    snap = _write_snapshot("sentences", all_rows)
    elapsed = time.perf_counter() - t0
    await emit(
        "embed.sentences.complete",
        embedded=done,
        total=total,
        snapshot=str(snap),
        seconds=round(elapsed, 1),
    )
    return {"embedded": done, "total": total, "snapshot": str(snap), "seconds": elapsed}


# ---------------------------------------------------------------------------
# HNSW index builds
# ---------------------------------------------------------------------------
_HNSW_STATEMENTS = (
    (
        "ix_p_embedding_semantic_hnsw",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_p_embedding_semantic_hnsw "
        "ON provision USING hnsw (embedding_semantic vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64);",
    ),
    (
        "ix_ps_embedding_hnsw",
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ps_embedding_hnsw "
        "ON provision_sentence USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64);",
    ),
)


async def build_hnsw_indexes() -> list[dict[str, Any]]:
    """Run CREATE INDEX CONCURRENTLY for each embedding column.

    CONCURRENTLY cannot run inside a transaction, so this uses an AUTOCOMMIT connection.
    Each statement is timed and reported.
    """
    engine = get_engine()
    results: list[dict[str, Any]] = []
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for name, sql in _HNSW_STATEMENTS:
            t0 = time.perf_counter()
            await emit("embed.index.start", name=name)
            try:
                await conn.execute(text(sql))
                elapsed = time.perf_counter() - t0
                results.append({"name": name, "ok": True, "seconds": round(elapsed, 1)})
                await emit("embed.index.complete", name=name, seconds=round(elapsed, 1))
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                results.append({"name": name, "ok": False, "error": repr(exc)})
                await emit("embed.index.failed", name=name, error=repr(exc), level="error")
    return results


# ---------------------------------------------------------------------------
# Sanity test: the first sentence of AIFMD Article 31 should retrieve its own provision
# ---------------------------------------------------------------------------
async def sanity_aifmd31() -> dict[str, Any]:
    """Check that AIFMD Article 31's first sentence retrieves its own provision first.

    The sentence is embedded and searched against provision.embedding_semantic; the check
    passes when Article 31 ranks first with cosine similarity above 0.9.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    async with session_scope() as session:
        # CELEX 32011L0061 is AIFMD; Article 31 provisions carry labels such as
        # "Article 31" or "Art. 31". Pick the longest match.
        row = (
            await session.execute(
                text(
                    """
                    SELECT p.id, p.label, p.text
                      FROM provision p
                      JOIN source_document sd ON sd.id = p.source_document_id
                     WHERE sd.external_id LIKE '%32011L0061%'
                       AND (p.label ILIKE 'Article 31%'
                            OR p.label ILIKE 'Art. 31%'
                            OR p.label = '31')
                     ORDER BY length(p.text) DESC
                     LIMIT 1
                    """
                )
            )
        ).first()

    if row is None:
        return {"ok": False, "reason": "AIFMD Art. 31 provision not found"}

    target_id, target_label, target_text = row
    first_sentence_text = await _first_sentence_text(target_id, target_text)

    client = get_openai()
    resp = await tracked_embedding(
        client,
        source="embed.sanity",
        model=EMBED_MODEL,
        input=first_sentence_text,
        dimensions=EMBED_DIM,
    )
    qvec = list(resp.data[0].embedding)

    async with session_scope() as session:
        top = (
            await session.execute(
                text(
                    """
                    SELECT id, label,
                           1 - (embedding_semantic <=> CAST(:q AS vector)) AS sim
                      FROM provision
                     WHERE embedding_semantic IS NOT NULL
                     ORDER BY embedding_semantic <=> CAST(:q AS vector)
                     LIMIT 5
                    """
                ),
                {"q": str(qvec)},
            )
        ).all()

    hits = [{"id": str(r[0]), "label": r[1], "similarity": float(r[2])} for r in top]
    ok = bool(hits) and hits[0]["id"] == str(target_id) and hits[0]["similarity"] > 0.9
    return {
        "ok": ok,
        "target_id": str(target_id),
        "target_label": target_label,
        "query_text": first_sentence_text[:200],
        "top5": hits,
    }


async def _first_sentence_text(provision_id: UUID, fallback_text: str) -> str:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(ProvisionSentence.text)
                .where(ProvisionSentence.provision_id == provision_id)
                .where(ProvisionSentence.kind == "sentence")
                .order_by(ProvisionSentence.sentence_index)
                .limit(1)
            )
        ).first()
    if row and row[0]:
        return row[0]
    # No sentence row: fall back to the first paragraph of the provision.
    head = fallback_text.strip().splitlines()[0] if fallback_text else ""
    return head or fallback_text[:500]


# ---------------------------------------------------------------------------
# Acceptance check
# ---------------------------------------------------------------------------
async def acceptance_status() -> dict[str, Any]:
    async with session_scope() as session:
        prov = (
            await session.execute(
                text(
                    "SELECT count(*) FILTER (WHERE embedding_semantic IS NOT NULL), count(*) "
                    "FROM provision"
                )
            )
        ).first()
        sent = (
            await session.execute(
                text(
                    "SELECT count(*) FILTER (WHERE embedding IS NOT NULL), count(*) "
                    "FROM provision_sentence"
                )
            )
        ).first()
        idx = (
            await session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename IN ('provision','provision_sentence') "
                    "  AND indexdef ILIKE '%hnsw%' "
                    "ORDER BY indexname"
                )
            )
        ).all()

    hnsw = [r[0] for r in idx]
    return {
        "provision_embedded": int(prov[0] or 0),
        "provision_total": int(prov[1] or 0),
        "sentence_embedded": int(sent[0] or 0),
        "sentence_total": int(sent[1] or 0),
        "hnsw_indexes": hnsw,
        "provision_complete": prov[0] == prov[1] and prov[1] > 0,
        "sentence_complete": sent[0] == sent[1] and sent[1] > 0,
        "hnsw_ok": len(hnsw) >= 2,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def _main_async(args: argparse.Namespace) -> None:
    if args.provisions or args.all:
        r = await embed_provisions()
        print(
            f"provisions: embedded={r['embedded']}/{r['total']} "
            f"snapshot={r.get('snapshot')} took={r.get('seconds', 0):.1f}s"
        )
    if args.sentences or args.all:
        r = await embed_sentences()
        print(
            f"sentences: embedded={r['embedded']}/{r['total']} "
            f"snapshot={r.get('snapshot')} took={r.get('seconds', 0):.1f}s"
        )
    if args.indexes or args.all:
        ix = await build_hnsw_indexes()
        for r in ix:
            print(
                f"index {r['name']}: ok={r['ok']} took={r.get('seconds', '-')}s "
                f"{r.get('error', '')}".rstrip()
            )
    if args.sanity or args.all:
        s = await sanity_aifmd31()
        print(f"sanity AIFMD Art. 31: ok={s.get('ok')}")
        for h in s.get("top5", []):
            print(f"  sim={h['similarity']:.4f}  {h['label']}  {h['id']}")
        if "reason" in s:
            print(f"  reason: {s['reason']}")
    if args.acceptance or args.all:
        a = await acceptance_status()
        print(
            f"acceptance: provision {a['provision_embedded']}/{a['provision_total']} "
            f"(ok={a['provision_complete']}), sentence {a['sentence_embedded']}/"
            f"{a['sentence_total']} (ok={a['sentence_complete']}), hnsw={a['hnsw_indexes']} "
            f"(ok={a['hnsw_ok']})"
        )


def main() -> None:
    configure_logging()
    p = argparse.ArgumentParser(description="Day-6: embed corpus + build HNSW indexes.")
    p.add_argument("--all", action="store_true", help="Run every step end-to-end.")
    p.add_argument("--provisions", action="store_true", help="Embed pending provisions.")
    p.add_argument("--sentences", action="store_true", help="Embed pending sentences.")
    p.add_argument("--indexes", action="store_true", help="Build HNSW indexes CONCURRENTLY.")
    p.add_argument("--sanity", action="store_true", help="AIFMD Art. 31 rank-1 check.")
    p.add_argument("--acceptance", action="store_true", help="Print BLOCKING acceptance state.")
    args = p.parse_args()
    if not any(
        [args.all, args.provisions, args.sentences, args.indexes, args.sanity, args.acceptance]
    ):
        p.print_help()
        return
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
