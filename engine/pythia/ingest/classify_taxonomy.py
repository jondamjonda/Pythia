"""Apply the seven-axis legal taxonomy to every source document (schema and population).

The controlled vocabulary lives in `pythia/graph/taxonomy.py`; this module handles the database
side: an additive schema migration, an idempotent population pass, a dry-run preview (classify,
validate, preview, and only then write) and a conformance report that fails on any
out-of-vocabulary value. `--down` restores the legacy `legal_hierarchy` and `document_type` values
from their backup in `metadata` and drops the new columns.

Run (from engine/): .venv/bin/python -m pythia.ingest.classify_taxonomy [--apply] [--down] [--all]
      (dry-run preview by default; --all also reclassifies already-tagged documents)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date

from pythia.db import close_pool, get_pool
from pythia.graph import taxonomy as tx

# Additive only: five new axis columns plus two doctrinal fields. legal_hierarchy and document_type
# already exist and are normalised in place. GIN index on legal_domain[], b-tree on faceting axes.
_DDL = """
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS bindingness            text;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS territorial_scope      text;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS temporal_status        text;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS issuing_authority      text;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS legal_domain           text[] DEFAULT '{}';
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS bindingness_contested  boolean DEFAULT false;
ALTER TABLE source_document ADD COLUMN IF NOT EXISTS doctrinal_note         text;
CREATE INDEX IF NOT EXISTS ix_sd_bindingness   ON source_document (bindingness);
CREATE INDEX IF NOT EXISTS ix_sd_tx_hierarchy  ON source_document (legal_hierarchy);
CREATE INDEX IF NOT EXISTS ix_sd_legal_domain  ON source_document USING gin (legal_domain);
"""

_DOWN = """
UPDATE source_document
   SET legal_hierarchy = COALESCE(metadata->>'_legacy_legal_hierarchy', legal_hierarchy),
       document_type   = COALESCE(metadata->>'_legacy_document_type',   document_type),
       metadata = (metadata - '_legacy_legal_hierarchy' - '_legacy_document_type' - '_taxonomy_v')
 WHERE metadata ? '_taxonomy_v';
ALTER TABLE source_document DROP COLUMN IF EXISTS bindingness;
ALTER TABLE source_document DROP COLUMN IF EXISTS territorial_scope;
ALTER TABLE source_document DROP COLUMN IF EXISTS temporal_status;
ALTER TABLE source_document DROP COLUMN IF EXISTS issuing_authority;
ALTER TABLE source_document DROP COLUMN IF EXISTS legal_domain;
ALTER TABLE source_document DROP COLUMN IF EXISTS bindingness_contested;
ALTER TABLE source_document DROP COLUMN IF EXISTS doctrinal_note;
"""

# Stored as metadata._taxonomy_v. Bump it when the classification changes, then rerun with --all
# (by default, already-tagged documents are skipped).
_TAXONOMY_V = "nt1-1"


async def _classify_corpus(conn, only_unclassified: bool) -> list[tuple]:
    """Classify every source_document and return validated UPDATE tuples.

    Classification is pure. Every value is checked against the controlled vocabulary before anything
    is returned, so an out-of-vocabulary value aborts the run instead of being written.
    """
    where = " WHERE NOT (metadata ? '_taxonomy_v')" if only_unclassified else ""
    rows = await conn.fetch(
        f"SELECT id, external_id, title, publisher, jurisdiction, legal_hierarchy, document_type, "
        f"valid_from, valid_to, metadata FROM source_document{where}"
    )
    today = date.today()
    out, bad = [], []
    for r in rows:
        c = tx.classify(
            title=r["title"],
            publisher=r["publisher"],
            jurisdiction=r["jurisdiction"],
            legacy_doctype=r["document_type"],
            legacy_hierarchy=r["legal_hierarchy"],
            valid_from=r["valid_from"],
            valid_to=r["valid_to"],
            as_of=today,
        )
        # validate against the controlled vocabulary
        for axis, val in (
            ("legal_hierarchy", c.legal_hierarchy),
            ("document_type", c.document_type),
            ("bindingness", c.bindingness),
            ("territorial_scope", c.territorial_scope),
            ("temporal_status", c.temporal_status),
        ):
            if not tx.is_valid(axis, val):
                bad.append((r["external_id"], axis, val))
        raw_md = r["metadata"]
        md = dict(json.loads(raw_md) if isinstance(raw_md, str) else (raw_md or {}))
        md.setdefault("_legacy_legal_hierarchy", r["legal_hierarchy"])
        md.setdefault("_legacy_document_type", r["document_type"])
        md["_taxonomy_v"] = _TAXONOMY_V
        out.append((r["id"], c, json.dumps(md)))
    if bad:
        raise ValueError(f"taxonomy conformance FAILED — out-of-vocab values: {bad[:10]}")
    return out


async def apply_taxonomy(*, apply: bool = False, only_unclassified: bool = True) -> dict:
    """Classify documents and, if `apply`, write the taxonomy; return the distribution per axis.

    With `only_unclassified` (the default) documents already tagged are skipped. The taxonomy is
    stored on `source_document` (the instrument level); consumers read most axes (bindingness,
    is_authoritative, temporal_status) through a provision-to-source_document join. The legacy
    `legal_hierarchy` and `document_type` columns are normalised in place to the controlled
    vocabulary, which is safe because nothing filters on them and the old values are backed up in
    `metadata`. The provision-level `legal_hierarchy`, which the engine reads directly, is
    normalised by the companion `cascade_provision_tier.py`.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if apply:
            await conn.execute(_DDL)
        plan = await _classify_corpus(conn, only_unclassified=only_unclassified)
        if apply:
            async with conn.transaction():
                for sid, c, md in plan:
                    await conn.execute(
                        """UPDATE source_document SET
                              legal_hierarchy=$2, document_type=$3, bindingness=$4, territorial_scope=$5,
                              temporal_status=$6, issuing_authority=$7, legal_domain=$8,
                              bindingness_contested=$9, doctrinal_note=$10, metadata=$11::jsonb
                           WHERE id=$1""",
                        sid,
                        c.legal_hierarchy,
                        c.document_type,
                        c.bindingness,
                        c.territorial_scope,
                        c.temporal_status,
                        c.issuing_authority,
                        c.legal_domain,
                        c.bindingness_contested,
                        c.doctrinal_note,
                        md,
                    )

        # report from the plan (works for both dry-run and apply)
        def dist(getter):
            d: dict[str, int] = {}
            for _, c, _md in plan:
                d[getter(c)] = d.get(getter(c), 0) + 1
            return dict(sorted(d.items(), key=lambda kv: -kv[1]))

        return {
            "mode": "APPLIED" if apply else "DRY-RUN (no write)",
            "n": len(plan),
            "legal_hierarchy": dist(lambda c: c.legal_hierarchy),
            "bindingness": dist(lambda c: c.bindingness),
            "document_type": dist(lambda c: c.document_type),
            "territorial_scope": dist(lambda c: c.territorial_scope),
            "temporal_status": dist(lambda c: c.temporal_status),
            "contested_or_noted": sum(1 for _, c, _ in plan if c.bindingness_contested or c.doctrinal_note),
        }


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DOWN)
        print("⏪ reverted: legacy legal_hierarchy/document_type restored; taxonomy columns dropped.")
        await close_pool()
        return
    res = await apply_taxonomy(apply=("--apply" in sys.argv), only_unclassified=("--all" not in sys.argv))
    print(f"=== taxonomy classification — {res['mode']} · {res['n']} documents ===")
    for axis in ("legal_hierarchy", "bindingness", "document_type", "territorial_scope", "temporal_status"):
        print(f"  {axis}:")
        for k, v in res[axis].items():
            print(f"     {k:24s} {v}")
    print(f"  contested/noted (doctrinal): {res['contested_or_noted']}")
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --apply to write)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
