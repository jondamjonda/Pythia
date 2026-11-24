"""Generate corpus/corpus_manifest.json, the committed inventory of every ingestable source act.

Case law is excluded; it has its own pipeline based on Ministry of Justice open data. The manifest is
built from the live database, so it also captures acts that were ingested individually and appear
in no other source file (for example the Czech acts 499/2004, 262/2011 and 89/1995), the Charter of
Fundamental Rights (2/1993) and the full EU layer. Each act's domain is taken from the committed
corpus/corpus_plan_p9.json where available. Re-run after the corpus changes to refresh the manifest.

Run (from engine/):  python scripts/build_manifest.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
from collections import Counter

# This script lives in scripts/; put engine/ on sys.path so `pythia` imports from a plain checkout.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pythia.db import close_pool, get_pool  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent.parent / "corpus"  # holds the plan and the manifest
_CELEX = re.compile(r"^3\d{4}[RL]")


def connector_of(ext: str | None, publisher: str | None) -> str:
    if _CELEX.match(ext or ""):
        return "eurlex"
    if publisher == "Sbírka zákonů":
        return "esbirka"
    if publisher == "ČNB":
        return "cnb_vestnik"
    if (publisher or "").startswith("ÚSC:"):
        return "regional"
    return "other"


async def main() -> None:
    plan = json.loads((HERE / "corpus_plan_p9.json").read_text(encoding="utf-8"))
    prov = {a["ext"]: a.get("domain") for a in plan.get("prioritized_ingest", [])}
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT external_id, title, legal_hierarchy, document_type, publisher
                 FROM source_document
                WHERE legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                ORDER BY (external_id ~ '^3[0-9]{4}[RL]'), external_id"""
        )
    acts = [
        {
            "external_id": r["external_id"],
            "title": r["title"],
            "connector": connector_of(r["external_id"], r["publisher"]),
            "legal_hierarchy": r["legal_hierarchy"],
            "document_type": r["document_type"],
            "publisher": r["publisher"],
            "domain": prov.get(r["external_id"]),
        }
        for r in rows
    ]
    counts = dict(Counter(a["connector"] for a in acts))
    manifest = {
        "schema_version": 1,
        "generated_from": "live pythia DB (source_document, non-CASE_LAW) + corpus_plan_p9.json provenance",
        "note": (
            "Authoritative inventory of ingestable source acts. rebuild_corpus.py re-ingests the "
            "esbirka + eurlex rows in the load-bearing order; cnb_vestnik + regional + case-law have "
            "their own connectors (see REBUILD.md). The PRIMARY recovery path is RESTORING the DB "
            "dump, not rebuilding — Qwen-extracted defined_terms are non-deterministic and only the "
            "dump preserves them faithfully."
        ),
        "counts": {**counts, "total": len(acts)},
        "acts": acts,
    }
    (HERE / "corpus_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("wrote corpus_manifest.json:", counts, "total", len(acts))
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
