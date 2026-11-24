"""Registry of the Czech financial-services acts, grouped by subdomain, for e-Sbírka ingestion.

Other legal domains (tax, criminal, administrative and so on) are listed in `cz_domains`.
Ingest every act not yet present (no LLM involved; uses ``pythia.ingest.esbirka``):

    .venv/bin/python -m pythia.ingest.cz_financial [--dry-run]    (from engine/)

Afterwards run the resolvers and materialize to wire the new acts into the graph.
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool
from pythia.ingest.esbirka import CzechActSpec, ingest_act


def _a(year: int, number: int, short: str, title: str) -> CzechActSpec:
    return CzechActSpec(year, number, short, title, "cz-act", "act")


# Subdomain to acts.
PERIMETER: dict[str, list[CzechActSpec]] = {
    "banking & central bank": [
        _a(1992, 21, "Banking", "Zákon č. 21/1992 Sb., o bankách"),
        _a(1993, 6, "ČNB", "Zákon č. 6/1993 Sb., o České národní bance"),
        _a(1995, 87, "Credit Unions", "Zákon č. 87/1995 Sb., o spořitelních a úvěrních družstvech"),
        _a(
            2015,
            374,
            "Recovery&Resolution",
            "Zákon č. 374/2015 Sb., o ozdravných postupech a řešení krize na finančním trhu",
        ),
        _a(2011, 136, "Banknote Circulation", "Zákon č. 136/2011 Sb., o oběhu bankovek a mincí"),
    ],
    "capital markets & funds": [
        _a(2004, 256, "ZPKT", "Zákon č. 256/2004 Sb., o podnikání na kapitálovém trhu"),
        _a(2013, 240, "ZISIF", "Zákon č. 240/2013 Sb., o investičních společnostech a investičních fondech"),
        _a(1998, 15, "CapMkt Supervision", "Zákon č. 15/1998 Sb., o dohledu v oblasti kapitálového trhu"),
        _a(2004, 190, "Bonds", "Zákon č. 190/2004 Sb., o dluhopisech"),
        _a(2008, 104, "Takeover Bids", "Zákon č. 104/2008 Sb., o nabídkách převzetí"),
    ],
    "insurance": [
        _a(2009, 277, "Insurance", "Zákon č. 277/2009 Sb., o pojišťovnictví"),
        _a(2018, 170, "Insurance Distribution", "Zákon č. 170/2018 Sb., o distribuci pojištění a zajištění"),
    ],
    "pensions & savings": [
        _a(2011, 427, "Supplementary Pension", "Zákon č. 427/2011 Sb., o doplňkovém penzijním spoření"),
        _a(1994, 42, "Pension Supplementary", "Zákon č. 42/1994 Sb., o penzijním připojištění se státním příspěvkem"),
        _a(1993, 96, "Building Savings", "Zákon č. 96/1993 Sb., o stavebním spoření"),
    ],
    "payments & credit": [
        _a(2017, 370, "Payments", "Zákon č. 370/2017 Sb., o platebním styku"),
        _a(2016, 257, "Consumer Credit", "Zákon č. 257/2016 Sb., o spotřebitelském úvěru"),
        _a(2013, 277, "Currency Exchange", "Zákon č. 277/2013 Sb., o směnárenské činnosti"),
    ],
    "AML, sanctions & consumer": [
        _a(
            2008, 253, "AML", "Zákon č. 253/2008 Sb., o některých opatřeních proti legalizaci výnosů z trestné činnosti"
        ),
        _a(2006, 69, "Intl Sanctions", "Zákon č. 69/2006 Sb., o provádění mezinárodních sankcí"),
        _a(1992, 634, "Consumer Protection", "Zákon č. 634/1992 Sb., o ochraně spotřebitele"),
    ],
    "accounting, audit & conglomerates": [
        _a(1991, 563, "Accounting", "Zákon č. 563/1991 Sb., o účetnictví"),
        _a(2009, 93, "Auditors", "Zákon č. 93/2009 Sb., o auditorech"),
        _a(
            2005,
            377,
            "Financial Conglomerates",
            "Zákon č. 377/2005 Sb., o doplňkovém dohledu nad finančními konglomeráty",
        ),
    ],
    "corporate & insolvency": [
        _a(2012, 90, "Business Corporations", "Zákon č. 90/2012 Sb., o obchodních korporacích"),
        _a(
            2008, 125, "Company Transformations", "Zákon č. 125/2008 Sb., o přeměnách obchodních společností a družstev"
        ),
        _a(2006, 182, "Insolvency", "Zákon č. 182/2006 Sb., o úpadku a způsobech jeho řešení"),
    ],
}


async def ingest_missing(dry_run: bool = False) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        have = {r["external_id"] for r in await conn.fetch("SELECT external_id FROM source_document")}
    all_acts = [s for grp in PERIMETER.values() for s in grp]
    todo = [s for s in all_acts if f"{s.number}/{s.year}" not in have]
    print(
        f"financial-services perimeter: {len(all_acts)} acts · {len(all_acts) - len(todo)} present · {len(todo)} to ingest"
    )
    ok = 0
    for s in todo:
        if dry_run:
            print(f"  (dry) {s.number}/{s.year:<7} {s.short} — {s.title[:48]}")
            continue
        try:
            r = await ingest_act(s)
            ok += 1
            print(f"OK  {s.number}/{s.year:<7} {s.short:<24} prov={r['n_provisions']:>4} xrefs={r['n_cross_refs']:>4}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR {s.number}/{s.year} {s.short}: {repr(exc)[:60]}")
    if not dry_run:
        print(
            f"\ningested {ok}/{len(todo)}. NEXT: resolve_internal + resolve_xrefs + materialize to wire into the graph."
        )
    await close_pool()


if __name__ == "__main__":
    asyncio.run(ingest_missing("--dry-run" in sys.argv))
