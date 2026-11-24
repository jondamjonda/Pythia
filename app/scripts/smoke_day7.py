"""Smoke test for the hybrid retrieval primitives against the live corpus.

Runs the metadata filter, dense and sparse search (provision and sentence level) and reciprocal
rank fusion for one query, then prints the fused top 10 for a manual check. Not a pytest test,
because it needs the fully ingested and embedded database.

Usage (from app/):
    python -m scripts.smoke_day7                                      # canonical AIFMD query
    python -m scripts.smoke_day7 "AIFMD marketing notification CZ"
"""

from __future__ import annotations

import asyncio
import sys

# PowerShell on Windows defaults to cp1250; force UTF-8 so Czech text renders.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from openai import AsyncOpenAI

from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_embedding
from regsuper.retrieval import (
    RetrievalFilters,
    dense_search,
    metadata_filter_ids,
    reciprocal_rank_fusion,
    sparse_search,
)


async def _embed(client: AsyncOpenAI, q: str) -> list[float]:
    resp = await tracked_embedding(
        client,
        source="smoke.day7",
        model="text-embedding-3-large",
        input=q,
        dimensions=1536,
    )
    return list(resp.data[0].embedding)


async def main(query: str) -> None:
    settings = get_settings()
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY not configured")

    print(f"query: {query}\n")
    filters = RetrievalFilters(jurisdictions=["EU", "CZ"])

    eligible = await metadata_filter_ids(filters, limit=10000)
    print(f"metadata_filter eligible (EU+CZ, active): {len(eligible)}\n")

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    qvec = await _embed(client, query)

    dense_prov = await dense_search(qvec, k=50, filters=filters, level="provision")
    dense_sent = await dense_search(qvec, k=50, filters=filters, level="sentence")
    sparse_prov = await sparse_search(query, k=50, filters=filters, level="provision")
    sparse_sent = await sparse_search(query, k=50, filters=filters, level="sentence")

    for name, hits in [
        ("dense.provision", dense_prov),
        ("dense.sentence", dense_sent),
        ("sparse.provision", sparse_prov),
        ("sparse.sentence", sparse_sent),
    ]:
        print(f"{name}: {len(hits)} hits, top score = {hits[0].score if hits else None}")

    fused = reciprocal_rank_fusion(
        [dense_prov, dense_sent, sparse_prov, sparse_sent],
        k=60,
        top_n=10,
    )
    print(
        f"\nRRF top 10 (of {len(set(h.provision_id for s in [dense_prov, dense_sent, sparse_prov, sparse_sent] for h in s))} unique):\n"
    )

    # Add labels to the top 10 for the manual check.
    from sqlalchemy import text

    from regsuper.storage.database import session_scope

    ids = [h.provision_id for h in fused]
    if ids:
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
        meta = {r[0]: r for r in rows}
        for rank, h in enumerate(fused, start=1):
            m = meta.get(h.provision_id)
            label = f"{m[2]}  {m[1]}  ({m[3][:50]})" if m else "?"
            contribs = ", ".join(f"{s}:{r}" for s, r in h.contributions)
            print(f"  {rank:>2}. score={h.score:.5f}  {label}")
            print(f"      contribs: {contribs}")


if __name__ == "__main__":
    q = (
        sys.argv[1]
        if len(sys.argv) > 1
        else ("How many days notice for an EU AIFM marketing to the CNB?")
    )
    asyncio.run(main(q))
