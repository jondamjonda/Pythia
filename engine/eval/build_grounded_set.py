"""Build a grounded multi-hop evaluation set from the structure of the law, with no lawyer or LLM.

Each item's gold provisions come from tables that are independent of the retriever being tested:
resolved citations (`cross_reference`), definitions (`defined_term`) and the article / § hierarchy.
Recovering them is therefore a fair test of whether a method can follow the real links in the law.
The query is built from the anchor provision's own salient terms, so the sparse seeder finds it.

This measures connectivity recall. That is enough to compare graph-expansion methods on recall@k
and to catch regressions, but it does not replace a lawyer-labelled relevance set, which is still
needed for final sign-off. Item types:

  xact  cross-act: citing provision, its target in another act, the target's paragraphs (2 hops)
  cite  intra-act: a provision and the provisions it cross-references (1 hop)
  defn  definition: the defining provision and provisions that use the term (1 hop)
  hier  scope: an EU article and its paragraphs (1 hop)

Run (from engine/):  .venv/bin/python eval/build_grounded_set.py
Writes eval/legal_grounded_set.jsonl.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re

from pythia.db import close_pool, get_pool

_OUT = pathlib.Path(__file__).parent / "legal_grounded_set.jsonl"
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_STOP = {
    "this",
    "that",
    "with",
    "from",
    "shall",
    "which",
    "such",
    "have",
    "been",
    "their",
    "other",
    "under",
    "where",
    "into",
    "they",
    "than",
    "when",
    "these",
    "those",
    "article",
    "paragraph",
    "podle",
    "tohoto",
    "tento",
    "tato",
    "této",
    "které",
    "který",
    "která",
    "nebo",
    "také",
    "jsou",
    "není",
    "podle",
    "ustanovení",
    "odstavce",
    "pokud",
    "tom",
    "při",
    "pro",
    "jako",
    "být",
    "musí",
    "může",
    "více",
    "kterou",
    "kterým",
    "kterých",
    "jiné",
    "jiného",
    "vztahuje",
}


def terms(text: str, n: int = 12) -> str:
    out, seen = [], set()
    for w in _WORD.findall(text.lower()):
        if w not in _STOP and w not in seen:
            seen.add(w)
            out.append(w)
        if len(out) >= n:
            break
    return " ".join(out)


def g(ext: str, label: str) -> str:
    return f"{ext} {label}"


async def main() -> None:
    pool = await get_pool()
    items: list[dict] = []
    async with pool.acquire() as c:
        # xact: cross-act citation, its target and the target's paragraphs (2 hops)
        rows = await c.fetch("""
            SELECT fp.label fl, fsd.external_id fe, fp.text ft, tp.id tid, tp.label tl, tsd.external_id te
            FROM cross_reference cr
            JOIN provision fp ON fp.id=cr.from_provision JOIN source_document fsd ON fsd.id=fp.source_document_id
            JOIN provision tp ON tp.id=cr.to_provision  JOIN source_document tsd ON tsd.id=tp.source_document_id
            WHERE cr.verified_by='pythia-xref-resolver' AND fsd.external_id <> tsd.external_id
              AND length(fp.text) BETWEEN 120 AND 2200
            ORDER BY fsd.external_id, fp.label LIMIT 7""")
        for i, r in enumerate(rows, 1):
            kids = await c.fetch("SELECT label FROM provision WHERE parent_id=$1 ORDER BY ordinal LIMIT 5", r["tid"])
            gold = [g(r["fe"], r["fl"]), g(r["te"], r["tl"])] + [g(r["te"], k["label"]) for k in kids]
            items.append(
                {
                    "id": f"xact-{i:02d}",
                    "type": "xact",
                    "query": "Související povinnosti a odkazovaná ustanovení v jiných předpisech: " + terms(r["ft"]),
                    "jurisdiction": ["CZ", "EU"],
                    "gold": sorted(set(gold)),
                    "gold_hops": 2,
                    "difficulty": "hard",
                    "note": f"cross-act: {r['fe']} {r['fl']} -> {r['te']} {r['tl']} (+paragraphs); gold from resolved cross_reference + hierarchy",
                }
            )

        # cite: a provision and its resolved internal cross-references (1 hop)
        rows = await c.fetch("""
            SELECT fp.label fl, sd.external_id fe, fp.text ft,
                   array_agg(tp.label ORDER BY tp.label) tls
            FROM cross_reference cr
            JOIN provision fp ON fp.id=cr.from_provision JOIN source_document sd ON sd.id=fp.source_document_id
            JOIN provision tp ON tp.id=cr.to_provision
            WHERE cr.ref_type='internal' AND cr.to_provision IS NOT NULL AND fp.id <> cr.to_provision
            GROUP BY fp.id, fp.label, sd.external_id, fp.text
            HAVING count(*) BETWEEN 3 AND 6 AND length(fp.text) BETWEEN 150 AND 2200
            ORDER BY sd.external_id, fp.label LIMIT 8""")
        for i, r in enumerate(rows, 1):
            gold = [g(r["fe"], r["fl"])] + [g(r["fe"], t) for t in r["tls"]]
            items.append(
                {
                    "id": f"cite-{i:02d}",
                    "type": "cite",
                    "query": "Která ustanovení tato úprava odkazuje a jak spolu souvisí: " + terms(r["ft"]),
                    "jurisdiction": ["CZ", "EU"],
                    "gold": sorted(set(gold)),
                    "gold_hops": 1,
                    "difficulty": "medium",
                    "note": f"intra-act citations of {r['fe']} {r['fl']}; gold from cross_reference",
                }
            )

        # defn: the defining provision and provisions that use the term (1 hop)
        rows = await c.fetch("""
            SELECT d.term, dp.id did, dp.label dl, sd.external_id de, dp.text dt
            FROM defined_term d JOIN provision dp ON dp.id=d.definition_provision_id
            JOIN source_document sd ON sd.id=dp.source_document_id
            WHERE d.definition_provision_id IS NOT NULL
              AND length(d.term) BETWEEN 4 AND 40 AND length(dp.text) BETWEEN 80 AND 2200
            ORDER BY sd.external_id, dp.label LIMIT 10""")
        di = 0
        for r in rows:
            users = await c.fetch(
                """SELECT p.label, sd.external_id e FROM pythia_edge ed
                JOIN provision p ON p.id=ed.src JOIN source_document sd ON sd.id=p.source_document_id
                WHERE ed.dst=$1 AND ed.edge_type='uses_term' ORDER BY p.label LIMIT 4""",
                r["did"],
            )
            if not users:
                continue
            di += 1
            gold = [g(r["de"], r["dl"])] + [g(u["e"], u["label"]) for u in users]
            items.append(
                {
                    "id": f"defn-{di:02d}",
                    "type": "defn",
                    "query": f"Definice a použití pojmu „{r['term']}“: " + terms(r["dt"]),
                    "jurisdiction": ["CZ", "EU"],
                    "gold": sorted(set(gold)),
                    "gold_hops": 1,
                    "difficulty": "medium",
                    "note": f"term '{r['term']}' defined in {r['de']} {r['dl']}; gold from defined_term + uses_term",
                }
            )
            if di >= 8:
                break

        # hier: an article and its paragraphs (1 hop)
        rows = await c.fetch("""
            SELECT p.id pid, p.label pl, sd.external_id pe, p.text pt
            FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
            WHERE p.label ~ '^Article [0-9]+$'
              AND EXISTS (SELECT 1 FROM provision ch WHERE ch.parent_id=p.id)
              AND length(p.text) BETWEEN 60 AND 2200
            ORDER BY sd.external_id, p.label LIMIT 6""")
        for i, r in enumerate(rows, 1):
            kids = await c.fetch("SELECT label FROM provision WHERE parent_id=$1 ORDER BY ordinal LIMIT 6", r["pid"])
            gold = [g(r["pe"], r["pl"])] + [g(r["pe"], k["label"]) for k in kids]
            items.append(
                {
                    "id": f"hier-{i:02d}",
                    "type": "hier",
                    "query": "Rozsah a jednotlivé odstavce tohoto článku: " + terms(r["pt"]),
                    "jurisdiction": ["EU"],
                    "gold": sorted(set(gold)),
                    "gold_hops": 1,
                    "difficulty": "easy",
                    "note": f"scope of {r['pe']} {r['pl']} + paragraphs; gold from hierarchy",
                }
            )

    _OUT.write_text("\n".join(json.dumps(it, ensure_ascii=False) for it in items) + "\n")
    by_type: dict[str, int] = {}
    for it in items:
        by_type[it["type"]] = by_type.get(it["type"], 0) + 1
    print(f"wrote {len(items)} items -> {_OUT.name}  {by_type}")
    print(f"  gold sizes: min {min(len(i['gold']) for i in items)}, max {max(len(i['gold']) for i in items)}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
