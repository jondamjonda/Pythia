#!/usr/bin/env python3
"""Populate provision_id_map, the crosswalk from regsuper provisions to Nexus (pythia) provisions.

The engine has no crosswalk endpoint, but both corpora are Postgres databases, so the crosswalk is
a deterministic join on (source_document.external_id, provision.label): no UUID guessing and no
embedding similarity. Only unique matches are written; absent or ambiguous matches are reported
and left unmapped rather than guessed. When last measured, all 7,895 regsuper provisions (11 acts)
matched uniquely.

The write touches no provision or finding data, is idempotent (a clean rebuild in one transaction)
and is reversible with `--truncate`. Populating the map changes no behaviour by itself: nothing
reads it until the cutover is enabled (law_source='nexus' together with cutover_approved).

Usage (from app/):
    python scripts/sync_regsuper_from_nexus.py              # dry run: coverage report only
    python scripts/sync_regsuper_from_nexus.py --apply      # populate provision_id_map
    python scripts/sync_regsuper_from_nexus.py --truncate   # empty the map (rollback)
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter, defaultdict

import asyncpg

from regsuper.corpus import NEXUS_CORPUS

# Provisions carry an act key (source_document.external_id) and a label within the act
# (provision.label); together they identify a provision across the two corpora. Anchor and null-key
# rows are excluded, since there is nothing to map.
_PROVISION_KEY_SQL = """
SELECT sd.external_id AS ext, p.label AS label, p.id::text AS id
  FROM provision p
  JOIN source_document sd ON sd.id = p.source_document_id
 WHERE sd.external_id IS NOT NULL AND p.label IS NOT NULL
"""


def _norm(s: str | None) -> str:
    """Collapse whitespace; the join is exact on the canonical (external_id, label) form."""
    return " ".join((s or "").split())


def _dsns() -> tuple[str, str]:
    """Return (regsuper_dsn, pythia_dsn) as plain asyncpg DSNs.

    The regsuper DSN comes from the app config or DATABASE_URL. The pythia DSN comes from
    PYTHIA_DATABASE_URL, otherwise it is derived from the regsuper DSN by swapping the database name
    (the local layout).
    """
    try:
        from regsuper.config import get_settings

        rs = get_settings().database_url
    except Exception:
        rs = os.getenv("DATABASE_URL", "postgresql://regsuper:regsuper@localhost:5432/regsuper")
    rs = rs.replace("+asyncpg", "")
    py = os.getenv("PYTHIA_DATABASE_URL", "").replace("+asyncpg", "")
    if not py:
        py = rs.rsplit("/", 1)[0] + "/pythia"
    return rs, py


async def build_crosswalk(rc: asyncpg.Connection, pc: asyncpg.Connection) -> dict:
    """Compute the deterministic crosswalk and a coverage report; read-only."""
    rs_rows = await rc.fetch(_PROVISION_KEY_SQL)
    py_rows = await pc.fetch(_PROVISION_KEY_SQL)

    pmap: dict[tuple[str, str], list[str]] = defaultdict(list)
    for r in py_rows:
        pmap[(_norm(r["ext"]), _norm(r["label"]))].append(r["id"])

    pairs: list[tuple[str, str]] = []  # (regsuper_id, nexus_id), unique matches only
    unmapped: list[dict] = []  # absent or ambiguous; never written
    by_act_tot, by_act_map = Counter(), Counter()

    for r in rs_rows:
        key = (_norm(r["ext"]), _norm(r["label"]))
        cand = pmap.get(key, [])
        by_act_tot[r["ext"]] += 1
        if len(cand) == 1:  # exactly one pythia row: a deterministic match
            pairs.append((r["id"], cand[0]))
            by_act_map[r["ext"]] += 1
        else:
            unmapped.append(
                {
                    "id": r["id"],
                    "ext": r["ext"],
                    "label": r["label"],
                    "reason": "absent" if not cand else f"ambiguous({len(cand)})",
                }
            )

    total = len(rs_rows)
    return {
        "total": total,
        "mapped": len(pairs),
        "unmapped": len(unmapped),
        "pct": (100.0 * len(pairs) / total) if total else 0.0,
        "pairs": pairs,
        "unmapped_rows": unmapped,
        "by_act": {a: (by_act_map[a], by_act_tot[a]) for a in by_act_tot},
    }


def _print_report(cw: dict) -> None:
    print(f"\nregsuper provisions (mappable key): {cw['total']}")
    print(f"  uniquely mapped → pythia : {cw['mapped']}  ({cw['pct']:.1f}%)")
    print(f"  unmapped (absent/ambig)  : {cw['unmapped']}")
    print("\nper-act coverage (mapped/total) — each act maps to a cutover domain at analysis time:")
    for act, (m, t) in sorted(cw["by_act"].items(), key=lambda kv: -kv[1][1]):
        print(f"  {act:<16} {m:>5}/{t:<5} ({100 * m / t:5.1f}%)")
    if cw["unmapped_rows"]:
        print("\nfirst unmapped (left OUT of the map, never guessed):")
        for u in cw["unmapped_rows"][:10]:
            print(f"  {u['ext']} {u['label']} — {u['reason']}")


async def main() -> None:
    ap = argparse.ArgumentParser(
        description="populate the regsuper↔nexus provision crosswalk (B3-03)"
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="WRITE the crosswalk into provision_id_map (default: dry-run)",
    )
    ap.add_argument(
        "--truncate", action="store_true", help="empty provision_id_map (rollback) and exit"
    )
    args = ap.parse_args()

    rs_dsn, py_dsn = _dsns()
    rc = await asyncpg.connect(rs_dsn)
    try:
        if args.truncate:
            n = await rc.fetchval("SELECT count(*) FROM provision_id_map")
            await rc.execute("TRUNCATE provision_id_map")
            print(f"TRUNCATE provision_id_map — removed {n} rows.")
            return

        pc = await asyncpg.connect(py_dsn)
        try:
            cw = await build_crosswalk(rc, pc)
        finally:
            await pc.close()
        _print_report(cw)

        if not args.apply:
            print("\nDRY-RUN — nothing written. Re-run with --apply to populate provision_id_map.")
            return

        # Idempotent rebuild in one transaction: clear the table, then insert the unique pairs
        # (--truncate reverses it). nexus_corpus is pinned to NEXUS_CORPUS from regsuper.corpus, the
        # constant the L2/L3 crosswalk lookups read, so the write and the reads cannot drift.
        # synced_at defaults to now().
        async with rc.transaction():
            await rc.execute("TRUNCATE provision_id_map")
            await rc.executemany(
                "INSERT INTO provision_id_map (regsuper_id, nexus_id, nexus_corpus) "
                "VALUES ($1::uuid, $2::uuid, $3)",
                [(rid, nid, NEXUS_CORPUS) for rid, nid in cw["pairs"]],
            )
        n = await rc.fetchval("SELECT count(*) FROM provision_id_map")
        print(
            f"\nAPPLIED — provision_id_map now has {n} rows "
            f"({cw['unmapped']} provisions deliberately left unmapped). law_source stays 'regsuper' — "
            f"nothing reads the map until the cutover is gated on. Rollback: --truncate."
        )
    finally:
        await rc.close()


if __name__ == "__main__":
    asyncio.run(main())
