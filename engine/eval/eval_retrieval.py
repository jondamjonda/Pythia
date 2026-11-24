"""Seed-retrieval evaluation: measure the index (embedder and reranker) without graph expansion.

Compares sparse, dense and hybrid seed retrieval on the grounded gold set, so a change of embedder
(bge-m3 against the earlier local mpnet model) or of reranker is measured on its own. The bar for
switching embedders: bge-m3 must beat the local model in the hybrid configuration, which is the one
that ships. Metrics: recall@10, recall@20, MRR (first gold hit) and hit@5 (at least one gold
provision in the top 5).

Run (from engine/):  .venv/bin/python eval/eval_retrieval.py [grounded_set.jsonl]
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import statistics as st
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.seed import dense_seed, hybrid_seed, sparse_seed

_SET = pathlib.Path(__file__).parent / "legal_grounded_set.jsonl"


async def _resolve_gold(conn, gold_strs: list[str]) -> set[str]:
    ids: set[str] = set()
    for s in gold_strs:
        ext, label = s.split(" ", 1)
        pid = await conn.fetchval(
            """SELECT p.id::text FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE sd.external_id = $1 AND p.label = $2 LIMIT 1""",
            ext,
            label,
        )
        if pid:
            ids.add(pid)
    return ids


def _metrics(ranked: list[str], gold: set[str]) -> tuple[float, float, float, float]:
    if not gold:
        return (0.0, 0.0, 0.0, 0.0)
    r10 = len(set(ranked[:10]) & gold) / len(gold)
    r20 = len(set(ranked[:20]) & gold) / len(gold)
    mrr = next((1.0 / (i + 1) for i, x in enumerate(ranked) if x in gold), 0.0)
    hit5 = 1.0 if set(ranked[:5]) & gold else 0.0
    return (r10, r20, mrr, hit5)


def _variants():
    K = 20
    return {
        "sparse": lambda q, j: sparse_seed(q, k=K, jurisdiction=j),
        "dense:local": lambda q, j: dense_seed(q, k=K, jurisdiction=j, embedder="local"),
        "dense:bge": lambda q, j: dense_seed(q, k=K, jurisdiction=j, embedder="bge"),
        "hybrid:local": lambda q, j: hybrid_seed(q, k=K, jurisdiction=j, embedder="local"),
        "hybrid:bge": lambda q, j: hybrid_seed(q, k=K, jurisdiction=j, embedder="bge"),
        "hybrid:bge+rrk": lambda q, j: hybrid_seed(q, k=K, jurisdiction=j, embedder="bge", rerank=True),
    }


async def run(set_path: pathlib.Path) -> None:
    items = [json.loads(ln) for ln in set_path.read_text().splitlines() if ln.strip()]
    pool = await get_pool()
    variants = _variants()
    agg: dict[str, list[tuple[float, float, float, float]]] = {v: [] for v in variants}
    async with pool.acquire() as conn:
        golds = [await _resolve_gold(conn, it["gold"]) for it in items]
    print(f"eval set: {set_path.name} · {len(items)} items")
    for name, fn in variants.items():
        for it, gold in zip(items, golds):
            seeds = await fn(it["query"], it.get("jurisdiction", ["CZ", "EU"]))
            ranked = [s.provision_id for s in seeds]
            agg[name].append(_metrics(ranked, gold))
        print(f"  · {name} done", flush=True)
    print(f"\n{'variant':<16}{'R@10':>8}{'R@20':>8}{'MRR':>8}{'hit@5':>8}")
    base = None
    for name in variants:
        rows = agg[name]
        r10, r20, mrr, hit5 = (st.mean(a[i] for a in rows) for i in range(4))
        delta = f"  (ΔR@10 {r10 - base:+.3f})" if base is not None and name.startswith("hybrid") else ""
        if name == "hybrid:local":
            base = r10
        print(f"{name:<16}{r10:>8.3f}{r20:>8.3f}{mrr:>8.3f}{hit5:>8.3f}{delta}")
    await close_pool()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = _SET if not arg else (pathlib.Path(arg) if "/" in arg else _SET.parent / arg)
    asyncio.run(run(path))
