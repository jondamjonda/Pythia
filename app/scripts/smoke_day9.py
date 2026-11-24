"""Retrieval acceptance check: ranking, negative-control score and warm latency.

Three gates:
  1. Canonical query: the top 5 must include AIFMD Article 31 or 32 (32011L0061); the Cohere
     reranker is expected to pull these to the top.
  2. Negative-control query: the top rerank_score must be below 0.5 (the refusal threshold).
  3. End-to-end latency below 2.5 s on the second, cached run.

Runs both the in-process pipeline and the /internal/retrieve HTTP route, so the route's response
shape is exercised too. Usage (from app/): python scripts/smoke_day9.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import httpx

from regsuper.retrieval import retrieve

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


CANONICAL = "How many days notice for an EU AIFM marketing to the CNB?"
NEGATIVE = "How long is a Tesla Model 3 warranty?"

AIFMD_CELEX = "32011L0061"
ART_31_32 = (
    "Article 31",
    "Article 32",
    "Article 31(1)",
    "Article 31(2)",
    "Article 31(3)",
    "Article 31(4)",
    "Article 32(1)",
    "Article 32(2)",
    "Article 32(3)",
    "Article 32(4)",
)


def _hit_summary(rank: int, payload: dict[str, Any], rerank_score, rrf_score) -> str:
    label = payload.get("label", "?")
    ext = payload.get("external_id", "?")
    title = (payload.get("title") or "")[:50]
    rscore = f"{rerank_score:.4f}" if rerank_score is not None else "—"
    return f"  {rank:>2}. rerank={rscore}  rrf={rrf_score:.4f}  {ext} {label}  ({title})"


async def assess(query: str, *, expect_aifmd_topk: bool) -> dict[str, Any]:
    print(f"\n=== {query!r} ===")
    # Cold run
    cold = await retrieve(
        query,
        jurisdiction="EU+CZ",
        sector="fin_services",
        top_n_rrf=50,
        top_n_rerank=10,
        expand_top_n=5,
        use_cache=True,
    )
    # Cached run for the latency gate (rewrite and HyDE should hit the cache)
    warm = await retrieve(
        query,
        jurisdiction="EU+CZ",
        sector="fin_services",
        top_n_rrf=50,
        top_n_rerank=10,
        expand_top_n=5,
        use_cache=True,
    )

    payloads = warm.payloads
    print(f"  cold {cold.diagnostics.latency_ms} ms   warm {warm.diagnostics.latency_ms} ms")
    print(f"  breakdown (warm): {warm.diagnostics.latency_breakdown_ms}")
    print(f"  rerank_skipped: {warm.diagnostics.rerank_skipped}")
    print("  top-10 reranked:")
    aifmd_in_top5 = False
    for rank, hit in enumerate(warm.candidates, start=1):
        meta = payloads.get(hit.provision_id) or {}
        print(_hit_summary(rank, meta, hit.rerank_score, hit.rrf_score))
        if rank <= 5 and meta.get("external_id") == AIFMD_CELEX and meta.get("label") in ART_31_32:
            aifmd_in_top5 = True
    print(f"  expanded entries: {len(warm.expanded)}")

    top_rerank = warm.candidates[0].rerank_score if warm.candidates else None
    return {
        "query": query,
        "aifmd_in_top5": aifmd_in_top5,
        "top_rerank_score": top_rerank,
        "warm_latency_ms": warm.diagnostics.latency_ms,
        "cold_latency_ms": cold.diagnostics.latency_ms,
        "rerank_skipped": warm.diagnostics.rerank_skipped,
        "expect_aifmd_topk": expect_aifmd_topk,
    }


async def http_smoke() -> dict[str, Any]:
    """Exercise the /internal/retrieve route via HTTP, if the server is up."""
    url = "http://127.0.0.1:8002/internal/retrieve"
    body = {
        "query": CANONICAL,
        "jurisdiction": ["EU", "CZ"],
        "sector": "fin_services",
        "top_n_rerank": 5,
        "expand_top_n": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, json=body)
        return {"status": r.status_code, "n_results": len(r.json().get("results", []))}
    except (httpx.ConnectError, httpx.ReadTimeout) as exc:
        return {"status": "down", "detail": repr(exc)}


async def main() -> None:
    canonical_report = await assess(CANONICAL, expect_aifmd_topk=True)
    negative_report = await assess(NEGATIVE, expect_aifmd_topk=False)
    http_report = await http_smoke()

    print("\n========== Day-9 BLOCKING acceptance ==========")
    gate_1 = canonical_report["aifmd_in_top5"]
    print(
        f"  [{'PASS' if gate_1 else 'FAIL'}] canonical: AIFMD Art 31/32 in top-5  "
        f"(rerank_skipped={canonical_report['rerank_skipped']})"
    )

    top_neg = negative_report["top_rerank_score"]
    if top_neg is None:
        gate_2 = "SKIP (no rerank)"
    else:
        gate_2 = "PASS" if top_neg < 0.5 else "FAIL"
    print(f"  [{gate_2}] negative: top rerank_score < 0.5  (got {top_neg})")

    warm_ms = canonical_report["warm_latency_ms"]
    gate_3 = "PASS" if warm_ms < 2500 else "FAIL"
    print(f"  [{gate_3}] latency (warm) < 2500 ms  (got {warm_ms} ms)")

    print(f"  HTTP /internal/retrieve: {http_report}")
    print(
        json.dumps(
            {
                "canonical": canonical_report,
                "negative": negative_report,
                "http": http_report,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
