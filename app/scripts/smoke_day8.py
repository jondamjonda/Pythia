"""Smoke test for query rewriting, HyDE and the full retrieval orchestrator.

Runs two queries: the canonical positive, which should put AIFMD Articles 31 and 32 in the top 5,
and a negative control (a Tesla warranty question), which should produce weak top scores so the
refusal threshold rejects it.

Usage (from app/): python scripts/smoke_day8.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from regsuper.retrieval import retrieve
from regsuper.storage.database import session_scope

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


async def _label_lookup(ids: list) -> dict:
    if not ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.id, p.label, sd.external_id, sd.title "
                    "FROM provision p "
                    "JOIN source_document sd ON sd.id = p.source_document_id "
                    "WHERE p.id = ANY(:ids)"
                ),
                {"ids": ids},
            )
        ).all()
    return {r[0]: r for r in rows}


async def run(query: str) -> None:
    print(f"\n=== query: {query!r} ===")
    result = await retrieve(
        query,
        jurisdiction="EU+CZ",
        sector="fin_services",
        top_n=10,
    )
    d = result.diagnostics
    print(f"  variants ({len(d.variants)}):")
    for v in d.variants:
        print(f"    - {v}")
    print(f"  hypotheticals ({len(d.hypotheticals)}):")
    for h in d.hypotheticals:
        snippet = h.replace("\n", " ")[:160]
        print(f"    - {snippet}")
    print(
        f"  result_sets={d.n_result_sets}  unique={d.n_unique_candidates}  "
        f"total={d.latency_ms} ms  breakdown={d.latency_breakdown_ms}"
    )

    ids = [h.provision_id for h in result.candidates]
    meta = await _label_lookup(ids)
    print("\n  RRF top 10:")
    for rank, hit in enumerate(result.candidates, start=1):
        m = meta.get(hit.provision_id)
        label = f"{m[2]}  {m[1]}  ({m[3][:50]})" if m else "?"
        print(f"  {rank:>2}. score={hit.score:.5f}  {label}")


async def main() -> None:
    await run("How many days notice for an EU AIFM marketing to the CNB?")
    await run("How long is a Tesla Model 3 warranty?")


if __name__ == "__main__":
    asyncio.run(main())
