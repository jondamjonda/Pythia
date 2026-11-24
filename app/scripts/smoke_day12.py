"""Acceptance smoke test for the full L1-L4 analysis cascade over HTTP.

Creates a project and runs POST /api/projects/{id}/analyze for layers L2, L3 and L4. Passes when
there are at least 15 applicable findings and 25 obligations, and every finding cites at least
one provision and document.

Usage (from app/, with the API on 127.0.0.1:8002): python scripts/smoke_day12.py
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
        "name": "Day-12 BLOCKING · EU AIF marketing in CZ",
        "sector": "fin_services",
        "jurisdiction_scope": ["EU", "CZ"],
        "entity_type": "AIF",
        "declared_intent": (
            "EU AIFM marketing an EU alternative investment fund to professional "
            "investors in the Czech Republic. Need to understand pre-marketing "
            "rules, notification deadlines to ČNB, local depositary obligations, "
            "AML duties, and reporting obligations to ESMA."
        ),
        "declared_risks": ["passporting_friction", "local_depositary_cost"],
    }

    async with httpx.AsyncClient(timeout=600.0) as client:
        print("=== POST /api/projects ===")
        r = await client.post(f"{BASE}/api/projects", json=intake)
        r.raise_for_status()
        proj = r.json()
        pid = proj["id"]
        print(f"  id={pid}")
        print(f"  sector_tags={proj['sector_tags']}")
        print(f"  activity_tags={proj['activity_tags']}")

        print("\n=== POST /api/projects/{id}/analyze · L2+L3+L4 (limit=200) ===")
        body = {
            "top_k": 200,
            "limit": 200,
            "max_concurrency": 10,
            "force": False,
            "layers": ["L2", "L3", "L4"],
        }
        r = await client.post(f"{BASE}/api/projects/{pid}/analyze", json=body)
        r.raise_for_status()
        rep = r.json()
        print(json.dumps(rep, indent=2))

        # Findings inventory
        print("\n=== GET /api/projects/{id}/findings  (all layers) ===")
        r = await client.get(f"{BASE}/api/projects/{pid}/findings?limit=400")
        r.raise_for_status()
        findings = r.json()["findings"]

        by_type = Counter(f["finding_type"] for f in findings)
        by_layer = Counter(f["analysis_layer"] for f in findings)
        bad_cite = [f for f in findings if not f["cited_provisions"] or not f["cited_documents"]]

        print(f"  total findings: {len(findings)}")
        print(f"  by finding_type: {dict(by_type)}")
        print(f"  by analysis_layer: {dict(by_layer)}")
        print(f"  findings with empty citations (must be 0): {len(bad_cite)}")

        # Show 3 example obligations and 3 example gaps
        print("\n  --- 3 example obligations ---")
        obs = [f for f in findings if f["finding_type"] == "obligation"]
        for f in obs[:3]:
            p = f["payload"]
            print(f"  · {p.get('modal')} — {p.get('actor')} — {p.get('action')[:90]}")
            if p.get("deadline"):
                print(f"      deadline: {p['deadline']}")

        print("\n  --- 3 example gaps ---")
        gaps = [f for f in findings if f["finding_type"] == "gap"]
        for f in gaps[:3]:
            p = f["payload"]
            evidence_types = [e["evidence_type"] for e in p.get("missing_evidence", [])]
            print(f"  · missing: {evidence_types}")
            print(f"      → {f['statement'][:140]}")

        # Acceptance gates
        print("\n=== Day-12 BLOCKING acceptance ===")
        n_applicable_l2 = by_type.get("applicable", 0)
        n_obligation = by_type.get("obligation", 0)
        n_gap = by_type.get("gap", 0)
        g1 = n_applicable_l2 >= 15
        g2 = n_obligation >= 25
        g3 = n_gap >= 0
        g4 = len(bad_cite) == 0
        print(f"  [{'PASS' if g1 else 'FAIL'}] applicable >= 15  (got {n_applicable_l2})")
        print(f"  [{'PASS' if g2 else 'FAIL'}] obligation  >= 25  (got {n_obligation})")
        print(f"  [{'PASS' if g3 else 'FAIL'}] gap         >= 0   (got {n_gap})")
        print(f"  [{'PASS' if g4 else 'FAIL'}] every finding cited (got {len(bad_cite)} bad)")

        print("\nDay-12 smoke complete.")


if __name__ == "__main__":
    asyncio.run(main())
