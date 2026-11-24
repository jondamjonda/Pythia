r"""Add or remove all case law in one command.

Case law is optional: the statute corpus must work without it, and rulings can be added or removed
at any time. Doing that correctly takes several ordered steps, and a half-finished sequence leaves
the graph inconsistent, so this wrapper runs the whole sequence and stops at the first failure. It
never changes statute data, and the statute four-corners and candidate lanes exclude case law anyway
(include_case_law=False), so even mid-toggle a statute answer never contains case law.

Run (from engine/):
  status:  .venv/bin/python -m pythia.ingest.caselaw                 # what is present (no writes)
  add:     .venv/bin/python -m pythia.ingest.caselaw --apply  [--year Y --month M --day D --max-docs N]
  remove:  .venv/bin/python -m pythia.ingest.caselaw --remove
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

from pythia.db import close_pool, get_pool
from pythia.graph.intelligence import reindex_intelligence
from pythia.graph.materialize import materialize_layer_a
from pythia.ingest.embed_bge import embed_all
from pythia.ingest.justice_caselaw import down as justice_down
from pythia.ingest.justice_caselaw import ingest_day, ingest_range
from pythia.ingest.link_caselaw import down as link_down
from pythia.ingest.link_caselaw import link_caselaw
from pythia.ingest.provision_versioning import apply as backfill_provenance


async def case_law_presence() -> dict:
    """Count the CASE_LAW documents and provisions and the case-law edges the linker wrote.

    Used by `status` and the post-toggle summary to state whether case law is in the corpus.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        docs = await conn.fetchval("SELECT count(*) FROM source_document WHERE legal_hierarchy = 'CASE_LAW'")
        provs = await conn.fetchval("SELECT count(*) FROM provision WHERE legal_hierarchy = 'CASE_LAW'")
        edges = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE source = 'case_law'")
    return {"documents": docs, "provisions": provs, "edges": edges, "present": bool(docs or provs or edges)}


async def add_case_law(
    *,
    year: int,
    month: int,
    day: int,
    max_docs: int | None,
    start: date | None = None,
    end: date | None = None,
    max_total: int | None = None,
) -> None:
    """Add case law: ingest, link, embed, build provenance, re-materialize, reindex.

    The steps: ingest rulings, link them to the statutes they cite, embed, build provenance bundles,
    re-materialize Layer-A (the derived edge layer) and reindex the graph-intelligence layer; without
    the reindex n6_check fails because the centrality/community layer no longer matches the live
    edge set. Each step prints a one-line result so a half-completed run is obvious, and every step
    is idempotent, so re-running is safe. Pass `start` and `end` (optionally `max_total`) to harvest a
    date range; otherwise a single day is ingested. The embed step fills the bge-m3 vectors of the
    new ruling provisions; without it rulings are invisible to dense search and fail the
    "ruling provisions embedded" case-law check (N8 in n5_check).
    """
    print("➕ ADDING case law\n")
    print("  [1/6] ingesting rulings (MoJ justice-portal connector) …")
    if start is not None and end is not None:
        s = await ingest_range(start=start, end=end, max_total=max_total, apply=True)
        print(
            f"        kept {s['kept']} · stored {s['stored']} rulings over {s['days']} days "
            f"(scanned {s['scanned']}, errors {s['errors']})"
        )
    else:
        s = await ingest_day(year=year, month=month, day=day, max_docs=max_docs, apply=True)
        print(f"        kept {s['kept']} · stored {s['stored']} rulings (scanned {s['scanned']}, errors {s['errors']})")

    print("  [2/6] linking ruling cites → interprets/applies edges …")
    ls = await link_caselaw(dry_run=False)
    print(
        f"        resolved {ls['resolved_total']} · edges written {ls['edges_written']} "
        f"(dangling {ls['dangling_total']})"
    )

    print("  [3/6] embedding new ruling provisions (bge-m3, only_missing) …")
    ne = await embed_all(only_missing=True)
    print(f"        embedded {ne} new provisions")

    print("  [4/6] building D1 provenance bundles + version seeds (source=court) …")
    pv = await backfill_provenance(apply=True)
    print(f"        D1 provenance/version backfill: {pv}")

    print("  [5/6] re-materializing Layer-A (statute edges stay consistent) …")
    ms = await materialize_layer_a()
    print(f"        pythia_edge total now {ms['total_edges']:,}")

    print("  [6/6] reindexing N6 intelligence (centrality + community + fingerprint) …")
    rs = await reindex_intelligence(force=True)
    print(f"        {rs['mode']} · {rs.get('n')} nodes · {rs.get('n_comm')} regimes")


async def remove_case_law() -> None:
    """Remove case law: drop edges, delete ruling documents, re-materialize, reindex.

    Only the CASE_LAW documents and the edges and graph intelligence derived from them change. The
    re-materialize step clears the defined_term edges the deleted rulings would otherwise leave
    orphaned; it is what makes removal leave no trace. The reindex keeps n6_check passing, as in
    add_case_law.
    """
    print("➖ REMOVING case law\n")
    print("  [1/4] dropping case-law edges (interprets/applies/leaf) …")
    n_edges = await link_down()
    print(f"        removed {n_edges} case-law edges")

    print("  [2/4] deleting ruling documents + provisions …")
    d = await justice_down()
    print(f"        removed {d['deleted_documents']} documents · {d['deleted_provisions']} provisions")

    print("  [3/4] re-materializing Layer-A (clears orphan defined_term edges) …")
    ms = await materialize_layer_a()
    print(f"        pythia_edge total now {ms['total_edges']:,}")

    print("  [4/4] reindexing N6 intelligence (centrality + community + fingerprint) …")
    rs = await reindex_intelligence(force=True)
    print(f"        {rs['mode']} · {rs.get('n')} nodes · {rs.get('n_comm')} regimes")


def _main() -> None:
    ap = argparse.ArgumentParser(
        description="Case-law ON/OFF switch: add or remove ALL case law in one consistent sequence."
    )
    ap.add_argument("--apply", action="store_true", help="ADD case law (ingest → link → materialize → reindex)")
    ap.add_argument("--remove", action="store_true", help="REMOVE all case law (downs → materialize → reindex)")
    # Which rulings to add; the defaults select the single day used for the reference case-law set.
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--month", type=int, default=3)
    ap.add_argument("--day", type=int, default=15)
    ap.add_argument("--max-docs", type=int, default=None, help="cap kept rulings for a single day (default: no cap)")
    ap.add_argument("--start", type=str, default=None, help="range start YYYY-MM-DD (harvest a date range)")
    ap.add_argument("--end", type=str, default=None, help="range end YYYY-MM-DD (inclusive)")
    ap.add_argument("--max-total", type=int, default=None, help="cap total kept rulings across the range")
    args = ap.parse_args()
    if args.apply and args.remove:
        ap.error("choose ONE of --apply / --remove (not both)")

    async def run():
        before = await case_law_presence()
        print(
            f"case law currently {'PRESENT' if before['present'] else 'ABSENT'} "
            f"({before['documents']} docs · {before['provisions']} provisions · {before['edges']} edges)\n"
        )
        if args.apply:
            start = date.fromisoformat(args.start) if args.start else None
            end = date.fromisoformat(args.end) if args.end else None
            await add_case_law(
                year=args.year,
                month=args.month,
                day=args.day,
                max_docs=args.max_docs,
                start=start,
                end=end,
                max_total=args.max_total,
            )
        elif args.remove:
            await remove_case_law()
        else:
            print("status only — pass --apply to add or --remove to remove. Nothing written.")
            await close_pool()
            return
        after = await case_law_presence()
        print(
            f"\n✅ done. case law now {'PRESENT' if after['present'] else 'ABSENT'} "
            f"({after['documents']} docs · {after['provisions']} provisions · {after['edges']} edges)"
        )
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
