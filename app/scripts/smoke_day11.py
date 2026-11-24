"""Smoke test for L2 applicability: create a project, analyse 30 candidates, list findings.

Checks that POST /api/projects/{id}/analyze runs L1 and L2 end to end, that findings are stored
with non-empty cited_provisions and cited_documents (the audit trigger rejects empty citations),
that each payload carries the LLM verdict (applicable, confidence, reasoning), and that a re-run
is idempotent.

Usage (from app/, with the API on 127.0.0.1:8002): python scripts/smoke_day11.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://127.0.0.1:8002"


async def main() -> None:
    intake = {
        "name": "Day-11 L2 smoke · AIF marketing CZ",
        "sector": "fin_services",
        "jurisdiction_scope": ["EU", "CZ"],
        "entity_type": "AIF",
        "declared_intent": (
            "Marketing an EU alternative investment fund to professional investors "
            "in the Czech Republic; specifically interested in pre-marketing rules, "
            "notification deadlines to ČNB, and local depositary obligations."
        ),
        "declared_risks": ["passporting_friction", "local_depositary_cost"],
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        print("=== POST /api/projects ===")
        r = await client.post(f"{BASE}/api/projects", json=intake)
        r.raise_for_status()
        proj = r.json()
        pid = proj["id"]
        print(f"  id={pid}")
        print(f"  sector_tags={proj['sector_tags']}")
        print(f"  activity_tags={proj['activity_tags']}")

        print("\n=== POST /api/projects/{id}/analyze  (limit=30 candidates) ===")
        body = {"top_k": 200, "limit": 30, "max_concurrency": 8, "force": False}
        r = await client.post(f"{BASE}/api/projects/{pid}/analyze", json=body)
        r.raise_for_status()
        rep = r.json()
        print(json.dumps(rep, indent=2))

        print("\n=== GET /api/projects/{id}/findings ===")
        r = await client.get(f"{BASE}/api/projects/{pid}/findings?layer=L2&limit=50")
        r.raise_for_status()
        data = r.json()
        print(f"  n_findings: {data['n_findings']}")

        # Show 5 example findings with their verdict and citations
        for f in data["findings"][:5]:
            verdict = f["payload"].get("applicable")
            conf = f["payload"].get("confidence")
            print(
                f"  - id={f['id'][:8]}…  verdict={verdict}  "
                f"conf={conf:.2f}  cites={len(f['cited_provisions'])}prov "
                f"{len(f['cited_documents'])}doc"
            )
            print(f"      statement: {f['statement'][:140]}")

        # Verdict distribution
        verdicts = Counter(f["payload"].get("applicable") for f in data["findings"])
        print(f"\n  Verdict distribution: {dict(verdicts)}")

        # Citation invariant: every finding must have non-empty citation arrays
        bad_cite = [
            f for f in data["findings"] if not f["cited_provisions"] or not f["cited_documents"]
        ]
        print(
            f"  Findings with empty citations: {len(bad_cite)}  "
            f"(must be 0 — audit trigger should have blocked these)"
        )

        # Idempotency: the second call should skip everything
        print("\n=== POST /api/projects/{id}/analyze  (idempotent re-run) ===")
        r = await client.post(f"{BASE}/api/projects/{pid}/analyze", json=body)
        r.raise_for_status()
        rep2 = r.json()
        print(f"  n_evaluated={rep2['n_evaluated']}  n_skipped_cached={rep2['n_skipped_cached']}")
        print("  (expect evaluated=0 + skipped=30 — audit log records ALL prior runs)")

    print("\nDay-11 smoke complete.")


if __name__ == "__main__":
    asyncio.run(main())
