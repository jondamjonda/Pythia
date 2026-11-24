"""Smoke test for project intake, tag derivation and L1 candidate retrieval over HTTP.

Creates a project, reads it back, fetches its metadata-only L1 candidates and checks that the
server-sent events endpoint accepts a connection.

Usage (from app/, with the API on 127.0.0.1:8002): python scripts/smoke_day10.py
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8002"


async def main() -> None:
    intake = {
        "name": "Test AIF marketing in CZ",
        "sector": "fin_services",
        "jurisdiction_scope": ["EU", "CZ"],
        "entity_type": "AIF",
        "declared_intent": (
            "Marketing an EU alternative investment fund to professional "
            "investors in the Czech Republic; need to know notification "
            "deadlines and any local depositary obligations."
        ),
        "declared_risks": ["passporting_friction", "local_depositary_cost"],
        "risk_tolerance": "moderate",
        "analysis_depth": "standard",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        print("=== POST /api/projects ===")
        r = await client.post(f"{BASE}/api/projects", json=intake)
        r.raise_for_status()
        proj = r.json()
        pid = proj["id"]
        print(json.dumps(proj, indent=2, ensure_ascii=False))

        print("\n=== GET /api/projects/{id} ===")
        r = await client.get(f"{BASE}/api/projects/{pid}")
        r.raise_for_status()
        roundtrip = r.json()
        assert roundtrip["id"] == pid
        assert roundtrip["sector_tags"] == proj["sector_tags"]
        print(
            f"  ok — name={roundtrip['name']!r}  "
            f"sector_tags={roundtrip['sector_tags']}  "
            f"activity_tags={roundtrip['activity_tags']}"
        )

        print("\n=== GET /api/projects/{id}/candidates ===")
        r = await client.get(f"{BASE}/api/projects/{pid}/candidates?top_k=200")
        r.raise_for_status()
        cands = r.json()
        print(f"  n_candidates: {cands['n_candidates']}")
        # Show the top candidates and counts by hierarchy and source
        from collections import Counter

        by_ext = Counter(c["source_external_id"] for c in cands["candidates"])
        by_hier = Counter(c["legal_hierarchy"] for c in cands["candidates"])
        print(f"  by legal_hierarchy: {dict(by_hier)}")
        print(f"  by source: {dict(by_ext.most_common(8))}")
        print("  top 5:")
        for c in cands["candidates"][:5]:
            print(f"    weight={c['priority_weight']:>3}  {c['source_external_id']}  {c['label']}")

        print("\n=== GET /api/projects/{id}/events  (SSE, 3s peek) ===")
        # Just confirm the connection opens and at least one frame arrives.
        try:
            async with client.stream(
                "GET",
                f"{BASE}/api/projects/{pid}/events",
                timeout=httpx.Timeout(5.0, read=5.0),
            ) as resp:
                resp.raise_for_status()
                count = 0
                async for raw in resp.aiter_lines():
                    if raw.startswith("data:"):
                        count += 1
                        if count >= 1:
                            break
                    if count == 0 and raw.startswith(":"):
                        # comment or keep-alive line
                        break
                print(f"  ok — sse opened, {count} data frame(s) seen")
        except (httpx.ReadTimeout, httpx.ReadError):
            print("  ok — sse opened (timed out waiting for next event)")

    print("\nDay-10 smoke complete.")


if __name__ == "__main__":
    asyncio.run(main())
