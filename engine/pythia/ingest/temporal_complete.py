"""Add temporal-completeness and identifier columns to source_document and populate them.

Adds is_authoritative, platnost, annulment_effect and ids_stable (described above ``_DDL``) with an
additive, reversible migration. Builds on `pythia/graph/temporal.py` and owns only the database side:
an idempotent population pass with a dry-run preview (derive, validate, preview, and only then write).

Run (from engine/): .venv/bin/python -m pythia.ingest.temporal_complete [--apply] [--down] [--all]
(dry run by default; --all also revisits documents that were already populated)
"""

from __future__ import annotations

import asyncio
import json
import sys

from pythia.db import close_pool, get_pool
from pythia.graph import temporal as tmp

# Columns added to source_document:
#   - is_authoritative: false for consolidated (KONSOL) text, which is informative only. This is the
#     headline trust flag the UI shows, so consolidated text is never presented as authentic.
#   - platnost: the formal-validity date, distinct from účinnost (effectiveness, valid_from and valid_to).
#     The e-Sbírka ingestion does not capture it yet, so it stays NULL rather than being fabricated until
#     ingestion backfills it.
#   - annulment_effect: ex nunc or ex tunc effect of a Constitutional Court annulment (NULL until
#     annulling case law is ingested).
#   - ids_stable: whether the document's e-Sbírka `frag_` ids were fetched after the cutoff from which they
#     are stable, so the structural locator and the frag id agree. Documents fetched before 2026-01-15
#     need re-resolution.
_DDL = """
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS is_authoritative  boolean;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS platnost          date;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS annulment_effect  text;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS ids_stable        boolean;
CREATE INDEX IF NOT EXISTS ix_sd_is_authoritative ON source_document (is_authoritative);
"""

_DOWN = """
UPDATE source_document SET metadata = metadata - '_temporal_v' WHERE metadata ? '_temporal_v';
ALTER TABLE source_document DROP COLUMN IF EXISTS is_authoritative;
ALTER TABLE source_document DROP COLUMN IF EXISTS platnost;
ALTER TABLE source_document DROP COLUMN IF EXISTS annulment_effect;
ALTER TABLE source_document DROP COLUMN IF EXISTS ids_stable;
"""

_TEMPORAL_V = "nt3-1"


def _md(raw) -> dict:
    return dict(json.loads(raw) if isinstance(raw, str) else (raw or {}))


async def _plan(conn, only_unset: bool):
    where = " WHERE NOT (metadata ? '_temporal_v')" if only_unset else ""
    rows = await conn.fetch(f"SELECT id, external_id, fetched_at, doctrinal_note, metadata FROM source_document{where}")
    out, bad = [], []
    for r in rows:
        md = _md(r["metadata"])
        authoritative = tmp.is_authoritative(md.get("typ_zneni"))
        ids_stable = tmp.frags_trustworthy(r["fetched_at"])
        annulment = None  # no annulled documents in the corpus yet; validated once there are
        if annulment is not None and annulment not in tmp.ANNULMENT_EFFECT:
            bad.append((r["external_id"], "annulment_effect", annulment))
        # attach the consolidated-text caveat to the taxonomy doctrinal_note when non-authoritative
        note = r["doctrinal_note"]
        if not authoritative and (not note or tmp.CONSOLIDATED_CAVEAT not in note):
            note = (note + " | " if note else "") + tmp.CONSOLIDATED_CAVEAT
        md["_temporal_v"] = _TEMPORAL_V
        out.append((r["id"], authoritative, None, annulment, ids_stable, note, json.dumps(md), r["fetched_at"]))
    if bad:
        raise ValueError(f"temporal conformance FAILED — out-of-vocab values: {bad[:10]}")
    return out


async def apply_temporal(*, apply: bool = False, only_unset: bool = True) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if apply:
            await conn.execute(_DDL)
        plan = await _plan(conn, only_unset=only_unset)
        if apply:
            async with conn.transaction():
                for sid, auth, plat, annul, ids_stable, note, md, _f in plan:
                    await conn.execute(
                        """UPDATE source_document SET is_authoritative=$2, platnost=$3,
                              annulment_effect=$4, ids_stable=$5, doctrinal_note=$6, metadata=$7::jsonb
                           WHERE id=$1""",
                        sid,
                        auth,
                        plat,
                        annul,
                        ids_stable,
                        note,
                        md,
                    )
        n = len(plan)
        return {
            "mode": "APPLIED" if apply else "DRY-RUN (no write)",
            "n": n,
            "authoritative_true": sum(1 for p in plan if p[1]),
            "authoritative_false": sum(1 for p in plan if not p[1]),
            "ids_stable_true": sum(1 for p in plan if p[4]),
            "needs_reresolution": sum(1 for p in plan if tmp.needs_reresolution(p[7])),
            "platnost_known": sum(1 for p in plan if p[2] is not None),
        }


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DOWN)
        print("⏪ reverted: temporal/identifier columns dropped; markers removed.")
        await close_pool()
        return
    res = await apply_temporal(apply=("--apply" in sys.argv), only_unset=("--all" not in sys.argv))
    print(f"=== temporal completeness — {res['mode']} · {res['n']} documents ===")
    print(
        f"  is_authoritative: {res['authoritative_true']} true / {res['authoritative_false']} false "
        f"(consolidated KONSOL = informative-only → false)"
    )
    print(
        f"  ids_stable (frags post-cutoff): {res['ids_stable_true']}/{res['n']} · "
        f"needs re-resolution (pre-2026-01-15): {res['needs_reresolution']}"
    )
    print(
        f"  platnost separately captured: {res['platnost_known']}/{res['n']} "
        f"(column ready; e-Sbírka ingestion to backfill)"
    )
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --apply to write)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
