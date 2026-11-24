"""Batch-ingest a list of Czech acts from e-Sbírka to expand the corpus safely.

Reads a JSON list of acts, ingests each act that is not yet in the database, tags its legal_domain
and writes a results file. Errors are handled per act: a wrong, renumbered or unavailable act is
logged and never stops the batch (the same approach as rebuild_corpus._ingest). Resumable and
idempotent: acts that already have provisions are skipped.

The follow-up chain (section rows, section kinds, enrichment, taxonomy classification, versioning,
tier cascade, two reindex passes and the integrity checks) is deliberately run separately after
ingestion, so the single global reindex runs once, in order, and each step can be verified.

List file (data/expansion_acts.json):
  [{"external_id":"586/1992","title":"...","domain":"tax","tier":1}, ...]

Run (from engine/):
  ingest tiers 1 and 2:          .venv/bin/python scripts/expand_ingest.py --tier 2
  ingest all:                    .venv/bin/python scripts/expand_ingest.py
  dry run (validate, no writes): .venv/bin/python scripts/expand_ingest.py --dry-run --tier 1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pythia.db import close_pool, get_pool  # noqa: E402
from pythia.ingest.esbirka import CzechActSpec, ingest_act  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
LIST = HERE / "data" / "expansion_acts.json"
RESULTS = HERE / "data" / "expansion_results.json"


def _spec(act: dict) -> CzechActSpec:
    number, year = act["external_id"].split("/")
    return CzechActSpec(
        int(year), int(number), act["external_id"], act.get("title", act["external_id"]), "cz-act", "act"
    )


async def main() -> None:
    ap = argparse.ArgumentParser(description="corpus-expansion batch ingester")
    ap.add_argument("--tier", type=int, default=3, help="ingest acts with tier <= this (default 3 = all)")
    ap.add_argument("--limit", type=int, default=0, help="cap the number of NEW ingestions (0 = no cap)")
    ap.add_argument("--dry-run", action="store_true", help="validate via store=False (fetch+parse, NO write)")
    args = ap.parse_args()

    acts = json.loads(LIST.read_text(encoding="utf-8"))
    acts = [a for a in acts if a.get("tier", 3) <= args.tier]
    acts.sort(key=lambda a: (a.get("tier", 3), a["external_id"]))
    print(f"list: {len(acts)} acts at tier ≤ {args.tier}  ({'DRY-RUN' if args.dry_run else 'INGEST'})", flush=True)

    pool = await get_pool()
    ingested, skipped, failed, did = [], [], [], 0
    for a in acts:
        ext = a["external_id"]
        async with pool.acquire() as c:
            present = await c.fetchval(
                "SELECT count(*) FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
                "WHERE sd.external_id=$1",
                ext,
            )
        if present and present > 0 and not args.dry_run:
            skipped.append(ext)
            continue
        if args.limit and did >= args.limit:
            break
        try:
            r = await ingest_act(_spec(a), store=not args.dry_run)
            did += 1
            n = r.get("n_provisions", 0)
            ingested.append({"external_id": ext, "n_provisions": n, "domain": a.get("domain")})
            print(
                f"  ✅ {ext:<10} {n:>4} provisions  [{a.get('domain', '?')}/t{a.get('tier', '?')}] {a.get('title', '')[:46]}",
                flush=True,
            )
            # Tag legal_domain straight away (idempotent, only if uncategorised); not in a dry run.
            if not args.dry_run and a.get("domain"):
                async with pool.acquire() as c:
                    await c.execute(
                        "UPDATE source_document SET legal_domain=$2::text[] "
                        "WHERE external_id=$1 AND (legal_domain='{uncategorized}' OR legal_domain IS NULL)",
                        ext,
                        [a["domain"]],
                    )
        except Exception as exc:  # one bad act must not abort the batch
            failed.append({"external_id": ext, "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
            print(f"  ✗ {ext:<10} {type(exc).__name__}: {str(exc)[:90]}", flush=True)

    res = {
        "tier_max": args.tier,
        "dry_run": args.dry_run,
        "n_ingested": len(ingested),
        "n_skipped": len(skipped),
        "n_failed": len(failed),
        "ingested": ingested,
        "failed": failed,
    }
    RESULTS.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"\n=== DONE — ingested {len(ingested)} · skipped(present) {len(skipped)} · failed {len(failed)} "
        f"→ {RESULTS.name} ===",
        flush=True,
    )
    if failed:
        print("  failures:", ", ".join(f["external_id"] for f in failed), flush=True)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
