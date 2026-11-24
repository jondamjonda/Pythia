"""Retrieval evaluation harness: score a retrieval method on a multi-hop gold set.

Gold provisions are stored as "<external_id> <label>", which does not depend on database ids, and
are resolved to ids at run time. Reports recall@10, recall@20 and recall@50 per item and on average.
The same harness takes larger, lawyer-labelled sets; the default set is a small verified starter set
that gives a baseline for the CTE and PageRank expansions, which the GNN has to beat.

Run (from engine/):  .venv/bin/python eval/run_eval.py [cte|ppr|gnn|both|all] [set-file]
Set SEED_RERANK=0 to score with the seeds the GNN was trained on (see `run`).
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import statistics as st
import sys

from pythia.contract import PythiaQuery
from pythia.db import close_pool, get_pool
from pythia.engine import PythiaEngine

_SET = pathlib.Path(__file__).parent / "legal_multihop_set.jsonl"


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


def _recall_at(ranked: list[str], gold: set[str], k: int) -> float:
    return len(set(ranked[:k]) & gold) / len(gold) if gold else 0.0


async def run(method: str, set_path: pathlib.Path = _SET) -> None:
    items = [json.loads(ln) for ln in set_path.read_text().splitlines() if ln.strip()]
    eng = PythiaEngine()
    # SEED_RERANK=0 turns off the cross-encoder seed rerank, so the GNN is scored on the same seed
    # distribution it was trained on (train_stage2.py seeds with rerank=False). By default the
    # reranked seeds used in production are kept. Running both ways gives a fair comparison.
    if os.environ.get("SEED_RERANK") == "0":
        eng.seed_rerank = False
    pool = await get_pool()
    print(f"\n=== method = {method} ===")
    print(f"{'item':<20}{'|gold|':>7}{'R@10':>7}{'R@20':>7}{'R@50':>7}{'nret':>6}")
    agg: list[tuple[float, float, float]] = []
    async with pool.acquire() as conn:
        for it in items:
            gold = await _resolve_gold(conn, it["gold"])
            ans = await eng.retrieve(
                PythiaQuery(query=it["query"], jurisdiction=it.get("jurisdiction", ["CZ", "EU"]), k=50),
                method=method,
            )
            ranked = [c.provision_id for c in ans.cited_provisions]
            r10, r20, r50 = _recall_at(ranked, gold, 10), _recall_at(ranked, gold, 20), _recall_at(ranked, gold, 50)
            agg.append((r10, r20, r50))
            print(f"{it['id']:<20}{len(gold):>7}{r10:>7.2f}{r20:>7.2f}{r50:>7.2f}{len(ranked):>6}")
    if agg:
        print(
            f"{'MEAN':<20}{'':>7}{st.mean(a[0] for a in agg):>7.2f}"
            f"{st.mean(a[1] for a in agg):>7.2f}{st.mean(a[2] for a in agg):>7.2f}"
        )


async def _main(arg: str, set_name: str | None) -> None:
    set_path = _SET if not set_name else (pathlib.Path(set_name) if "/" in set_name else _SET.parent / set_name)
    methods = {"both": ["cte", "ppr"], "all": ["cte", "ppr", "gnn"]}.get(arg, [arg])
    seed_mode = (
        "rerank=False (training-consistent)" if os.environ.get("SEED_RERANK") == "0" else "rerank=True (production)"
    )
    print(f"eval set: {set_path.name}   seeds: {seed_mode}")
    for m in methods:
        await run(m, set_path)
    await close_pool()


if __name__ == "__main__":
    # Usage: run_eval.py [cte|ppr|gnn|both|all] [set-file]
    asyncio.run(_main(sys.argv[1] if len(sys.argv) > 1 else "both", sys.argv[2] if len(sys.argv) > 2 else None))
