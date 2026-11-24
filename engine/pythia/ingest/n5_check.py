"""Case-law integrity check: every ``d1_check`` invariant plus the case-law invariants.

Ingesting court rulings must not break the temporal-versioning invariants, so those run first,
unchanged. The case-law checks (labels as printed in the PASS/FAIL table):

  N1  every CASE_LAW document has bindingness 'INTERPRETIVE_CASE_LAW' (never treated as binding)
  N2  every CASE_LAW bundle has source 'court' (never mis-sourced as e-Sbírka primary law)
  N3  no duplicate ECLIs (European Case Law Identifier, the canonical ruling id)
  N4  no fabrication: every source='case_law' edge with a dst points at a real provision
  N5  act scoping: every resolved case-law forward edge lands in the act the citation names
  N6  every case-law forward edge (interprets, applies, annuls) starts in a CASE_LAW provision
  N7  no double counting: a ruling citation is never also a generic `references` edge
  N8  every ruling provision is embedded, so it is retrievable
  N9  no pollution: the default four-corners expansion from a statute seed reaches no CASE_LAW
  N10 documents annulled by the Constitutional Court (ANNULLED_BY_CC) are well formed:
      annulment_effect is set and an annulled_by edge comes from a CASE_LAW provision

Run (from engine/): .venv/bin/python -m pythia.ingest.n5_check  (prints PASS/FAIL, exits 0 or 1)
"""

from __future__ import annotations

import asyncio
import json
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.cte_expand import four_corners
from pythia.ingest.d1_check import check as d1_check


async def check() -> dict:
    d1 = await d1_check()  # temporal-versioning invariants; rulings must not break them
    pool = await get_pool()
    async with pool.acquire() as conn:
        n_docs = await conn.fetchval("SELECT count(*) FROM source_document WHERE legal_hierarchy='CASE_LAW'")
        bad_binding = await conn.fetchval(
            "SELECT count(*) FROM source_document WHERE legal_hierarchy='CASE_LAW' "
            "AND bindingness IS DISTINCT FROM 'INTERPRETIVE_CASE_LAW'"
        )
        bad_source = await conn.fetchval(
            "SELECT count(*) FROM source_document WHERE legal_hierarchy='CASE_LAW' "
            "AND provenance->>'source' IS DISTINCT FROM 'court'"
        )
        dup_ecli = await conn.fetchval(
            "SELECT count(*) FROM (SELECT external_id FROM source_document WHERE legal_hierarchy='CASE_LAW' "
            "GROUP BY external_id HAVING count(*)>1) x"
        )
        fab = await conn.fetchval(
            "SELECT count(*) FROM pythia_edge e WHERE e.source='case_law' AND e.dst IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM provision p WHERE p.id=e.dst)"
        )
        cross_act = await conn.fetchval(
            """SELECT count(*) FROM pythia_edge e
                 JOIN provision p ON p.id=e.dst JOIN source_document sd ON sd.id=p.source_document_id
                WHERE e.source='case_law' AND e.edge_type IN ('interprets','applies','annuls')
                  AND sd.external_id IS DISTINCT FROM (e.metadata->>'act')"""
        )
        bad_src = await conn.fetchval(
            """SELECT count(*) FROM pythia_edge e
                 JOIN provision p ON p.id=e.src JOIN source_document sd ON sd.id=p.source_document_id
                WHERE e.source='case_law' AND e.edge_type IN ('interprets','applies','annuls')
                  AND sd.legal_hierarchy <> 'CASE_LAW'"""
        )
        double_count = await conn.fetchval(
            """SELECT count(*) FROM pythia_edge e
                WHERE e.source<>'case_law' AND e.edge_type IN ('references','referenced_by','internal_reference')
                  AND e.src IN (SELECT p.id FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                                WHERE sd.legal_hierarchy='CASE_LAW')"""
        )
        unembedded = await conn.fetchval(
            "SELECT count(*) FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
            "WHERE sd.legal_hierarchy='CASE_LAW' AND p.embedding_bge IS NULL"
        )
        # N10: ANNULLED_BY_CC documents are well formed (vacuously true until Constitutional Court
        # annulments are ingested).
        annul_docs = await conn.fetchval("SELECT count(*) FROM source_document WHERE temporal_status='ANNULLED_BY_CC'")
        annul_illformed = await conn.fetchval(
            """SELECT count(*) FROM source_document sd WHERE sd.temporal_status='ANNULLED_BY_CC'
                 AND (sd.annulment_effect IS NULL
                      OR NOT EXISTS (SELECT 1 FROM pythia_edge e JOIN provision p ON p.id=e.dst
                            JOIN source_document s2 ON s2.id=p.source_document_id
                           WHERE e.edge_type='annulled_by' AND e.source='case_law'
                             AND s2.id=sd.id
                             AND e.src IN (SELECT id FROM provision WHERE source_document_id IN
                                  (SELECT id FROM source_document WHERE legal_hierarchy='CASE_LAW'))))"""
        )
        # N9, no pollution: seed with statute provisions that rulings do cite (the worst case, taken from
        # the dst of case-law forward edges) and assert the default four-corners expansion surfaces no
        # CASE_LAW provision.
        seeds = [
            str(r["dst"])
            for r in await conn.fetch(
                """SELECT DISTINCT e.dst FROM pythia_edge e JOIN provision p ON p.id=e.dst
                 JOIN source_document sd ON sd.id=p.source_document_id
                WHERE e.source='case_law' AND e.edge_type IN ('interprets','applies')
                  AND sd.legal_hierarchy<>'CASE_LAW' AND e.dst IS NOT NULL LIMIT 8"""
            )
        ]
    leaked = 0
    for sid in seeds:
        hits = await four_corners([sid], max_hops=2, k=200)  # default: include_case_law=False
        if hits:
            ids = [h.provision_id for h in hits]
            async with pool.acquire() as conn:
                leaked += await conn.fetchval(
                    "SELECT count(*) FROM provision WHERE id = ANY($1::uuid[]) AND legal_hierarchy='CASE_LAW'", ids
                )
    async with pool.acquire() as conn:
        by_court = {
            r["issuing_authority"]: r["n"]
            for r in await conn.fetch(
                "SELECT issuing_authority, count(*) AS n FROM source_document WHERE legal_hierarchy='CASE_LAW' "
                "GROUP BY issuing_authority ORDER BY n DESC"
            )
        }
        n_edges = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE source='case_law'")

    n5 = {
        "N1 CASE_LAW bindingness=INTERPRETIVE (bad=0)": bad_binding == 0,
        "N2 CASE_LAW bundle source=court (bad=0)": bad_source == 0,
        "N3 0 duplicate ECLIs": dup_ecli == 0,
        "N4 no fabrication (case_law edge dst not a provision = 0)": fab == 0,
        "N5 act-scoping (cross-act snaps = 0)": cross_act == 0,
        "N6 case_law edge src is CASE_LAW (bad=0)": bad_src == 0,
        "N7 no double-count (ruling cite as references = 0)": double_count == 0,
        "N8 ruling provisions embedded (unembedded=0)": unembedded == 0,
        "N9 anti-pollution (CASE_LAW from STATUTE seed = 0)": leaked == 0,
        "N10 ANNULLED_BY_CC well-formed (illformed=0)": annul_illformed == 0,
    }
    checks = {**d1["checks"], **n5}
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "detail": {
            "d1_ok": d1["ok"],
            "n_case_law_docs": n_docs,
            "n_case_law_edges": n_edges,
            "bad_binding": bad_binding,
            "bad_source": bad_source,
            "dup_ecli": dup_ecli,
            "fabricated": fab,
            "cross_act_snaps": cross_act,
            "bad_src": bad_src,
            "double_count": double_count,
            "unembedded": unembedded,
            "pollution_leaked": leaked,
            "annulled_by_cc_docs": annul_docs,
            "annul_illformed": annul_illformed,
            "by_court": by_court,
        },
        "d1_detail": d1.get("detail"),
    }


async def _main() -> None:
    res = await check()
    print("=== N5 invariant harness (superset of d1_check) ===")
    for name, ok in res["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    d = res["detail"]
    print(
        f"\n  case law: {d['n_case_law_docs']} docs · {d['n_case_law_edges']} case_law edges · "
        f"by court: {d['by_court']}"
    )
    if not res["ok"]:
        print("  detail:", json.dumps(d, ensure_ascii=False, default=str))
    print(f"\n  {'✅ ALL N5 + D1 INVARIANTS HOLD' if res['ok'] else '❌ INVARIANT VIOLATION'}")
    await close_pool()
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    asyncio.run(_main())
