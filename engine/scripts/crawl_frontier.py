"""Ingest the most-cited Czech acts that the corpus references but does not yet contain.

Finds the Czech acts cited by the current corpus but not yet ingested (the frontier of the
"four corners" expansion), ranks them by how many provisions cite them, and ingests the top N into
the pythia database with the e-Sbírka fetcher. Repealed predecessors (often cited in transitional
provisions) are kept but flagged through `valid_to` (e-Sbírka `účinnost-do`): the current-law
filter hides them from default retrieval, while they stay reachable for historical (`as_of`)
questions. Refuses to run unless the configured database URL (PYTHIA_DATABASE_URL, which defaults
to the local pythia database) points at the pythia database.

Run (from engine/):  .venv/bin/python scripts/crawl_frontier.py [TOP_N]
"""

import asyncio
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import asyncpg

# This script lives in scripts/; put engine/ on sys.path so `pythia` imports from a plain checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pythia.config import get_settings  # noqa: E402
from pythia.ingest.esbirka import CzechActSpec, ingest_act  # noqa: E402

_CZ = re.compile(r"č\.\s*(\d+)/(\d+)\s*Sb\.", re.IGNORECASE)


async def main(top_n: int = 12, min_year: int = 1991) -> None:
    url = get_settings().database_url
    if "pythia" not in url:
        raise SystemExit("refusing to run: DATABASE_URL is not the pythia DB")
    dsn = url.replace("+asyncpg", "")
    conn = await asyncpg.connect(dsn)
    corpus = {r["external_id"] for r in await conn.fetch("SELECT external_id FROM source_document")}
    rows = await conn.fetch(
        """SELECT cr.to_external_ref t, count(DISTINCT cr.from_provision) n
             FROM cross_reference cr
            WHERE cr.ref_type='external' AND cr.to_provision IS NULL AND cr.to_external_ref IS NOT NULL
            GROUP BY cr.to_external_ref"""
    )
    freq: Counter = Counter()
    for r in rows:
        m = _CZ.search(r["t"] or "")
        if m:
            num, year = int(m.group(1)), int(m.group(2))
            ext = f"{num}/{year}"
            if ext not in corpus and year >= min_year:
                freq[(year, num)] += r["n"]
    targets = [k for k, _ in sorted(freq.items(), key=lambda kv: -kv[1])][:top_n]
    print(f"frontier: {len(freq)} missing CZ acts (year≥{min_year}); ingesting top {len(targets)}")
    today = date.today()
    ok = rep = err = 0
    for year, num in targets:
        spec = CzechActSpec(year, num, f"{num}/{year}", f"Zákon č. {num}/{year} Sb.", "cz-act", "act")
        try:
            res = await ingest_act(spec)
            vt = await conn.fetchval("SELECT valid_to FROM source_document WHERE external_id=$1", f"{num}/{year}")
            repealed = bool(vt and vt < today)
            ok += 1
            rep += repealed
            print(
                f"OK  {num}/{year:<5} prov={res.get('n_provisions'):<4} xrefs={res.get('n_cross_refs'):<4} "
                f"{'REPEALED' if repealed else 'current '} (valid_to={vt})"
            )
        except Exception as exc:  # noqa: BLE001
            err += 1
            print(f"ERR {num}/{year}: {repr(exc)[:70]}")
    await conn.close()
    print(f"\ndone: {ok} ingested ({rep} repealed-flagged), {err} errors")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 12))
