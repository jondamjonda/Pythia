"""Temporal-versioning integrity check for the live corpus database.

Asserts the invariants that provision versioning, amendment ingestion and effect dates rely on:
head uniqueness (one is_head row per document and provision label, backed by the uq_pv_head
index), a valid provenance bundle on every document, no pending re-seed (a dry run of
``provision_versioning.apply`` would version nothing twice), head/provision date agreement,
well-formed and non-overlapping version windows, end dates propagated from documents to their
provisions, and bundles that stay in sync with the source_document columns.

Run (from engine/): .venv/bin/python -m pythia.ingest.d1_check  (prints PASS/FAIL, exits 0 or 1)
"""

from __future__ import annotations

import asyncio
import json
import sys

from pythia.db import close_pool, get_pool
from pythia.graph import provenance as prov
from pythia.ingest.provision_versioning import apply as pv_apply


async def check() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        dup_heads = await conn.fetchval(
            "SELECT count(*) FROM (SELECT 1 FROM provision_version WHERE is_head "
            "GROUP BY source_document_id, provision_label HAVING count(*) > 1) x"
        )
        null_prov = await conn.fetchval("SELECT count(*) FROM source_document WHERE provenance IS NULL")
        status_disagree = await conn.fetchval(
            "SELECT count(*) FROM source_document WHERE provenance IS NOT NULL "
            "AND temporal_status IS DISTINCT FROM provenance->>'temporal_status'"
        )
        # Head/provision date agreement: provision_version.valid_from must equal provision.valid_from for
        # every head. This deliberately does not compare against the document bundle's effect date: an
        # amendment that changes only some provisions leaves the unchanged ones at their original valid_from
        # while the document's date advances, so that cross-level equality is not an invariant.
        # Document-level bundle consistency is covered by `bundle_drift` and `status_disagree`.
        three_way = await conn.fetchval(
            """SELECT count(*) FROM provision_version pv
                 JOIN provision p ON p.source_document_id=pv.source_document_id AND p.label=pv.provision_label
                WHERE pv.is_head AND pv.valid_from IS DISTINCT FROM p.valid_from"""
        )
        bundles = await conn.fetch("SELECT external_id, provenance FROM source_document WHERE provenance IS NOT NULL")
        bad = [
            b["external_id"]
            for b in bundles
            if not prov.is_valid(json.loads(b["provenance"]) if isinstance(b["provenance"], str) else b["provenance"])
        ]
        # Defect classes specific to the amendment path and valid_to propagation.
        inverted = await conn.fetchval(  # zero- or negative-width windows vanish from point-in-time reads
            "SELECT count(*) FROM provision_version WHERE valid_to IS NOT NULL AND valid_to <= valid_from"
        )
        overlap = await conn.fetchval(  # two versions of one label covering the same instant
            """SELECT count(*) FROM provision_version a JOIN provision_version b
                 ON a.source_document_id=b.source_document_id AND a.provision_label=b.provision_label
                    AND a.id < b.id
                WHERE a.valid_from IS NOT NULL AND b.valid_from IS NOT NULL
                  AND a.valid_from < COALESCE(b.valid_to, 'infinity'::date)
                  AND b.valid_from < COALESCE(a.valid_to, 'infinity'::date)"""
        )
        vt_not_propagated = await conn.fetchval(  # doc end date not copied to provision.valid_to (mask reads it)
            """SELECT count(*) FROM provision p JOIN source_document sd ON sd.id=p.source_document_id
                WHERE sd.valid_to IS NOT NULL AND p.valid_to IS NULL"""
        )
        bundle_drift = await conn.fetchval(  # bundle out of sync with source_document columns after an amendment
            """SELECT count(*) FROM source_document sd WHERE provenance IS NOT NULL AND (
                   sd.raw_checksum IS DISTINCT FROM (provenance->>'raw_checksum')
                OR sd.valid_to IS DISTINCT FROM NULLIF(provenance->>'valid_to','')::date
                OR (provenance->>'source' <> 'cnb'
                    AND sd.valid_from IS DISTINCT FROM NULLIF(provenance->>'valid_from','')::date))"""
        )
    dry = await pv_apply(apply=False)  # must be a no-op: 0 pending seeds
    pending_ok = dry["docs"] == 0 and dry["provisions"] == 0
    checks = {
        "head_uniqueness (dup heads = 0)": dup_heads == 0,
        "provenance present (null = 0)": null_prov == 0,
        "bundles conformant (invalid = 0)": len(bad) == 0,
        "no pending re-seed (apply dry-run = 0/0)": pending_ok,
        "three-way date agreement (violations = 0)": three_way == 0,
        "temporal_status column == bundle (disagree = 0)": status_disagree == 0,
        "no inverted windows (valid_to <= valid_from = 0)": inverted == 0,
        "no overlapping version windows per label (= 0)": overlap == 0,
        "doc valid_to propagated to provision (gap = 0)": vt_not_propagated == 0,
        "bundle tracks columns (drift = 0)": bundle_drift == 0,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "detail": {
            "dup_heads": dup_heads,
            "null_provenance": null_prov,
            "invalid_bundles": bad[:10],
            "n_invalid_bundles": len(bad),
            "apply_dry_run": dry,
            "three_way_violations": three_way,
            "status_disagree": status_disagree,
            "inverted_windows": inverted,
            "overlapping_windows": overlap,
            "valid_to_not_propagated": vt_not_propagated,
            "bundle_drift": bundle_drift,
        },
    }


async def _main() -> None:
    res = await check()
    print("=== D1 invariant harness ===")
    for name, ok in res["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    if not res["ok"]:
        print("\n  detail:", json.dumps(res["detail"], ensure_ascii=False, default=str))
    print(f"\n  {'✅ ALL INVARIANTS HOLD' if res['ok'] else '❌ INVARIANT VIOLATION'}")
    await close_pool()
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    asyncio.run(_main())
