"""Registry of the principal Czech acts outside financial services, for e-Sbírka ingestion.

The companion to `cz_financial`. Each domain lists its foundational code(s) and the key companion
acts: the backbone, not every act. Every entry is the current principal act (not a repealed
predecessor) and was checked to resolve on e-Sbírka. Ingest every act not yet present (no LLM
involved; uses the e-Sbírka ingester):

    .venv/bin/python -m pythia.ingest.cz_domains [--dry-run]    (from engine/)

Then run resolve_internal, resolve_xrefs, materialize, classify_taxonomy, temporal_complete and
cascade_provision_tier to wire the new acts into the graph and the taxonomy.
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool
from pythia.ingest.esbirka import CzechActSpec, ingest_act


def _a(year: int, number: int, short: str, title: str) -> CzechActSpec:
    return CzechActSpec(year, number, short, title, "cz-act", "act")


# Domain to principal acts. All were current and resolvable on e-Sbírka when checked (June 2026).
PERIMETER: dict[str, list[CzechActSpec]] = {
    "tax": [
        _a(1992, 586, "Income Tax", "Zákon č. 586/1992 Sb., o daních z příjmů"),
        _a(2004, 235, "VAT", "Zákon č. 235/2004 Sb., o dani z přidané hodnoty"),
        _a(2009, 280, "Tax Procedure Code", "Zákon č. 280/2009 Sb., daňový řád"),
        _a(2003, 353, "Excise Duties", "Zákon č. 353/2003 Sb., o spotřebních daních"),
        _a(1992, 338, "Real Estate Tax", "Zákon č. 338/1992 Sb., o dani z nemovitých věcí"),
    ],
    "criminal": [
        _a(2009, 40, "Criminal Code", "Zákon č. 40/2009 Sb., trestní zákoník"),
        # Dates from 1961 but is not repealed: still the current Criminal Procedure Code (trestní řád).
        _a(1961, 141, "Criminal Procedure", "Zákon č. 141/1961 Sb., o trestním řízení soudním (trestní řád)"),
        _a(
            2011,
            418,
            "Corporate Criminal Liability",
            "Zákon č. 418/2011 Sb., o trestní odpovědnosti právnických osob a řízení proti nim",
        ),
        _a(2003, 218, "Juvenile Justice", "Zákon č. 218/2003 Sb., o soudnictví ve věcech mládeže"),
        _a(2013, 45, "Victims Of Crime", "Zákon č. 45/2013 Sb., o obětech trestných činů"),
        _a(1999, 169, "Imprisonment Execution", "Zákon č. 169/1999 Sb., o výkonu trestu odnětí svobody"),
        _a(2000, 257, "Probation & Mediation", "Zákon č. 257/2000 Sb., o Probační a mediační službě"),
    ],
    "civil_procedure": [
        _a(1963, 99, "Civil Procedure Code", "Zákon č. 99/1963 Sb., občanský soudní řád"),
        _a(2013, 292, "Special Court Proceedings", "Zákon č. 292/2013 Sb., o zvláštních řízeních soudních"),
        _a(
            2001,
            120,
            "Enforcement Code",
            "Zákon č. 120/2001 Sb., o soudních exekutorech a exekuční činnosti (exekuční řád)",
        ),
        _a(1991, 549, "Court Fees", "Zákon č. 549/1991 Sb., o soudních poplatcích"),
        _a(2012, 91, "Private International Law", "Zákon č. 91/2012 Sb., o mezinárodním právu soukromém"),
    ],
    "procurement": [
        _a(2016, 134, "Public Procurement", "Zákon č. 134/2016 Sb., o zadávání veřejných zakázek"),
        _a(2015, 340, "Register Of Contracts", "Zákon č. 340/2015 Sb., o registru smluv"),
        # Repealed by 231/2025 Sb. with effect from 2027-01-01.
        _a(2001, 320, "Financial Control", "Zákon č. 320/2001 Sb., o finanční kontrole ve veřejné správě"),
        _a(
            1996,
            273,
            "Competition Authority",
            "Zákon č. 273/1996 Sb., o působnosti Úřadu pro ochranu hospodářské soutěže",
        ),
        _a(1993, 166, "Supreme Audit Office", "Zákon č. 166/1993 Sb., o Nejvyšším kontrolním úřadu"),
    ],
    "administrative": [
        _a(2004, 500, "Administrative Procedure", "Zákon č. 500/2004 Sb., správní řád"),
        _a(2002, 150, "Administrative Justice", "Zákon č. 150/2002 Sb., soudní řád správní"),
        _a(2016, 250, "Administrative Offences", "Zákon č. 250/2016 Sb., o odpovědnosti za přestupky a řízení o nich"),
        _a(2016, 251, "Specific Offences", "Zákon č. 251/2016 Sb., o některých přestupcích"),
        _a(2012, 255, "Inspection Code", "Zákon č. 255/2012 Sb., o kontrole (kontrolní řád)"),
        _a(2014, 234, "Civil Service", "Zákon č. 234/2014 Sb., o státní službě"),
    ],
    "labour": [
        _a(2006, 262, "Labour Code", "Zákon č. 262/2006 Sb., zákoník práce"),
        _a(2004, 435, "Employment", "Zákon č. 435/2004 Sb., o zaměstnanosti"),
        _a(2005, 251, "Labour Inspection", "Zákon č. 251/2005 Sb., o inspekci práce"),
        _a(
            2006,
            309,
            "OHS",
            "Zákon č. 309/2006 Sb., o zajištění dalších podmínek bezpečnosti a ochrany zdraví při práci",
        ),
        _a(1991, 2, "Collective Bargaining", "Zákon č. 2/1991 Sb., o kolektivním vyjednávání"),
    ],
}


async def ingest_missing(dry_run: bool = False) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        have = {r["external_id"] for r in await conn.fetch("SELECT external_id FROM source_document")}
    all_acts = [s for grp in PERIMETER.values() for s in grp]
    todo = [s for s in all_acts if f"{s.number}/{s.year}" not in have]
    print(f"CZ-domains perimeter: {len(all_acts)} acts · {len(all_acts) - len(todo)} present · {len(todo)} to ingest")
    ok = 0
    for s in todo:
        if dry_run:
            print(f"  (dry) {s.number}/{s.year:<7} {s.short:<26} — {s.title[:46]}")
            continue
        try:
            r = await ingest_act(s)
            ok += 1
            print(f"OK  {s.number}/{s.year:<7} {s.short:<26} prov={r['n_provisions']:>5} xrefs={r['n_cross_refs']:>5}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR {s.number}/{s.year} {s.short}: {repr(exc)[:70]}")
    if not dry_run:
        print(f"\ningested {ok}/{len(todo)}. NEXT: resolve_internal + resolve_xrefs + materialize + the NT passes.")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(ingest_missing("--dry-run" in sys.argv))
