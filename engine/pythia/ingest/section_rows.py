"""Materialise the structural ancestors of provisions (část, hlava, díl, oddíl) as anchor rows.

Trust is tagged per section (preamble, definitions, operative rules, penalties) and resolved per
provision at retrieval, so a section must be a real `provision` row that leaves inherit from via
`parent_id`. Ingestion keeps the outline of part (část), title (hlava), division (díl) and
subdivision (oddíl) only in each provision's `hierarchy_path` ltree. This additive backfill creates
one anchor row per unoccupied path prefix and points parentless leaves at their nearest anchor.

Run (from engine/):
  ensure  :  .venv/bin/python -m pythia.ingest.section_rows --ensure-schema   (columns only; run first)
  dry run :  .venv/bin/python -m pythia.ingest.section_rows
  apply   :  .venv/bin/python -m pythia.ingest.section_rows --backfill
  undo    :  .venv/bin/python -m pythia.ingest.section_rows --unmake   (aliases: --drop, --down)
              Add --drop-schema to also remove the anchor columns (see `unmake`).
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid

from pythia.db import close_pool, get_pool

# Single source for the heading derived from an ltree segment ('cast_10' becomes 'Část 10'), the
# same label graph.browse renders for these groups.
from pythia.graph.browse import _struct_label

_SOURCE = "section_anchor_b1a03"  # provenance tag on every materialized row (metadata.source)
# Fixed namespace for the deterministic anchor uuid5, so a re-run reproduces the same ids.
_NS = uuid.UUID("b1a0303c-0000-4000-8000-5ec71092a73c")
# provision.text_hash is NOT NULL; anchors carry the sha256 of their empty body.
_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


# --------------------------------------------------------------------------------------------- DDL
# Additive and idempotent. is_anchor_only excludes a row from retrieval; anchor_level is the ltree
# segment type (cast, hlava, dil, oddil, par, ...) that the trust layer keys section behaviour on.
# Anchors are a small minority, so a partial index on the TRUE rows is cheap; it serves both the
# anchor exclusion in the seed query and the --unmake cleanup.
_DDL = """
ALTER TABLE provision ADD COLUMN IF NOT EXISTS is_anchor_only boolean NOT NULL DEFAULT false;
ALTER TABLE provision ADD COLUMN IF NOT EXISTS anchor_level   text;
CREATE INDEX IF NOT EXISTS ix_provision_anchor_only ON provision (id) WHERE is_anchor_only;
"""


async def ensure_schema() -> None:
    """Add the anchor columns and index if missing (idempotent).

    The seed candidate query (graph.seed) and the four_corners walk (graph.cte_expand) reference
    `is_anchor_only`. With its default of false every real row is a non-anchor, so the predicate
    changes nothing until anchors exist, but the column must exist before those queries run. Call
    this once on a new database, before or without a full --backfill.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)


def _seg_type(segment: str) -> str:
    """Return the anchor_level token for a segment: 'cast_10' gives 'cast', 'preambule' stays as is."""
    return segment.partition("_")[0] or segment


def anchor_id(doc_id: uuid.UUID | str, prefix: str) -> uuid.UUID:
    """Return the deterministic id of the anchor at ltree `prefix` in document `doc_id`."""
    return uuid.uuid5(_NS, f"{doc_id}:{prefix}")


def _prefixes(path: str) -> list[str]:
    """Return every proper prefix of an ltree path, root first: 'a.b.c' gives ['a', 'a.b']."""
    segs = path.split(".")
    return [".".join(segs[: i + 1]) for i in range(len(segs) - 1)]


def plan_anchors(rows: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Plan the anchor rows and leaf repoints for one act (pure; no database access).

    Args:
        rows: the act's real provisions as dicts with id, hpath (ltree text), ordinal, valid_from,
            valid_to and doc_id.

    Returns:
        A pair (anchors, repoints). `anchors` has one dict per distinct prefix not occupied by a
        real provision, with its parent prefix, label, anchor_level and the minimum ordinal and
        valid_from of its descendants, sorted root first so a parent is always inserted before its
        children. `repoints` lists (leaf_id, parent_prefix) for every real row below the root; the
        caller repoints only leaves whose parent_id is currently NULL.
    """
    real_paths = {r["hpath"] for r in rows if r.get("hpath")}
    # Aggregate descendant ordinal and dates per prefix so an anchor sorts into reading order.
    agg: dict[str, dict] = {}
    doc_id = rows[0]["doc_id"] if rows else None
    repoints: list[tuple[str, str]] = []
    for r in rows:
        hp = r.get("hpath")
        if not hp:
            continue
        pref_list = _prefixes(hp)
        if pref_list:
            repoints.append((r["id"], pref_list[-1]))  # the leaf's immediate ancestor prefix
        for pref in pref_list:
            if pref in real_paths:
                continue  # a real EU article occupies it, so no anchor
            a = agg.setdefault(
                pref,
                {
                    "ordinal": r.get("ordinal"),
                    "valid_from": r.get("valid_from"),
                    "valid_to": r.get("valid_to"),
                    "any_open": r.get("valid_to") is None,
                },
            )
            if r.get("ordinal") is not None and (a["ordinal"] is None or r["ordinal"] < a["ordinal"]):
                a["ordinal"] = r["ordinal"]
            vf = r.get("valid_from")
            if vf is not None and (a["valid_from"] is None or vf < a["valid_from"]):
                a["valid_from"] = vf
            # An anchor spans its descendants: it is open (valid_to NULL) if any descendant is open,
            # otherwise it ends at the latest descendant valid_to. This preserves the d1_check rule
            # that a document's end date propagates to its provisions (in a fully repealed act every
            # descendant, and so every anchor, has a valid_to), while a partly in-force act keeps its
            # anchors open.
            vt = r.get("valid_to")
            if vt is None:
                a["any_open"] = True
            elif a["valid_to"] is None or vt > a["valid_to"]:
                a["valid_to"] = vt
    anchors: list[dict] = []
    for pref in sorted(agg, key=lambda p: (p.count("."), p)):  # root first: parents before children
        segs = pref.split(".")
        parent_prefix = ".".join(segs[:-1]) if len(segs) > 1 else None  # None marks a root anchor
        anchors.append(
            {
                "prefix": pref,
                "id": anchor_id(doc_id, pref),
                "parent_prefix": parent_prefix,  # resolved to an id at insert time (root-first order)
                "label": _struct_label(segs[-1]),
                "anchor_level": _seg_type(segs[-1]),
                "ordinal": agg[pref]["ordinal"],
                "valid_from": agg[pref]["valid_from"],
                # open if any descendant is in force, else the latest descendant valid_to
                "valid_to": None if agg[pref]["any_open"] else agg[pref]["valid_to"],
            }
        )
    return anchors, repoints


_FETCH_ACT_ROWS = """
    SELECT p.id::text AS id, p.hierarchy_path::text AS hpath, p.ordinal, p.valid_from, p.valid_to,
           p.source_document_id::text AS doc_id, p.parent_id
      FROM provision p
     WHERE p.source_document_id = $1
       AND p.is_anchor_only = false        -- plan from REAL rows only (re-run never re-anchors anchors)
       AND p.hierarchy_path IS NOT NULL
     ORDER BY p.ordinal NULLS LAST, p.hierarchy_path
"""


async def _backfill_act(conn, doc_id: str, *, apply: bool) -> dict:
    """Plan and, if `apply`, write the anchors for one act, then repoint its parentless leaves.

    Each anchor row gets:

      - is_anchor_only = TRUE, which excludes it from the seed candidate query (graph.seed) so an
        anchor never becomes a retrieval seed. Anchors have no text, tsv or embedding and could not
        match anyway; the flag makes the exclusion explicit and testable.
      - id = uuid5(namespace, source_document_id + ':' + prefix), so a re-run produces the same ids
        (ON CONFLICT DO NOTHING) and leaf-to-anchor pointers stay stable.
      - parent_id = the row at the next-shorter prefix (another anchor, or a real EU article that
        occupies it), so walking up parent_id climbs from leaf through sections to the root.
      - hierarchy_path = the prefix; ordinal and valid_from = the minimum over its descendants, which
        keeps reading order and the point-in-time mask consistent; label from _struct_label;
        anchor_level = the segment type (cast, hlava, ...).

    Leaves are repointed only where parent_id is NULL (the Czech leaves). EU acts already nest
    through parent_id (eurlex.py sets it; a paragraph sits under a real article), so no existing
    parent_id is overwritten and no text, edge or definition row is touched.
    """
    rows = [dict(r) for r in await conn.fetch(_FETCH_ACT_ROWS, doc_id)]
    if not rows:
        return {"anchors_planned": 0, "anchors_written": 0, "leaves_repointed": 0}
    null_parent = {r["id"] for r in rows if r["parent_id"] is None}
    anchors, repoints = plan_anchors(rows)
    if not apply:
        repointable = sum(1 for lid, _ in repoints if lid in null_parent)
        return {"anchors_planned": len(anchors), "anchors_written": 0, "leaves_repointed": repointable}
    pref_to_id = {a["prefix"]: a["id"] for a in anchors}
    written = 0
    # Insert root first so each anchor's parent is resolvable: the anchor at the parent prefix if one
    # was planned, else the real provision occupying that prefix.
    real_path_to_id = {r["hpath"]: r["id"] for r in rows}
    for a in anchors:
        pid = None
        if a["parent_prefix"] is not None:
            pid = pref_to_id.get(a["parent_prefix"]) or real_path_to_id.get(a["parent_prefix"])
        res = await conn.execute(
            """INSERT INTO provision
                   (id, source_document_id, parent_id, hierarchy_path, ordinal, label, text, text_format,
                    text_hash, is_anchor_only, anchor_level, valid_from, valid_to, language, jurisdiction,
                    legal_hierarchy, metadata)
               SELECT $1::uuid, $2::uuid, $3::uuid, $4::ltree, $5::int, $6::text,
                      ''::text,             -- anchors are STRUCTURAL carriers: empty body (provision.text
                                            -- is NOT NULL). No tsv/embedding is ever built for them, and the
                                            -- L1 candidate SQL excludes is_anchor_only anyway → never a seed.
                      'gfm', $11::text, true, $7::text, $8::date, $12::date,
                      src.language, src.jurisdiction, src.legal_hierarchy,
                      jsonb_build_object('source', $9::text, 'anchor_level', $7::text)
                 FROM provision src
                WHERE src.id = $10::uuid
               ON CONFLICT (id) DO NOTHING""",
            a["id"],
            doc_id,
            pid,
            a["prefix"],
            a["ordinal"],
            a["label"],
            a["anchor_level"],
            a["valid_from"],
            _SOURCE,
            rows[0]["id"],
            _EMPTY_HASH,
            a["valid_to"],
        )
        written += 1 if res.endswith(" 1") else 0
    # Repoint only leaves whose parent_id is NULL to their immediate ancestor anchor.
    repoint_params = []
    for lid, parent_prefix in repoints:
        if lid not in null_parent:
            continue
        anchor = pref_to_id.get(parent_prefix)
        if anchor is not None:
            repoint_params.append((anchor, lid))
    repointed = 0
    if repoint_params:
        await conn.executemany(
            "UPDATE provision SET parent_id = $1::uuid WHERE id = $2::uuid AND parent_id IS NULL", repoint_params
        )
        repointed = len(repoint_params)
    return {"anchors_planned": len(anchors), "anchors_written": written, "leaves_repointed": repointed}


async def backfill(*, apply: bool = False) -> dict:
    """Create section anchors for every act and repoint parentless leaves (a dry run unless `apply`).

    Section and article rows are leaves: for Czech acts parent_id is NULL and the outline exists only
    in hierarchy_path (e.g. 'd_89_2012.cast_1.hlava_2.dil_3.par_118'); graph.browse.build_act_tree
    rebuilds the nesting at read time but never stores it. Re-ingesting acts to add ancestor rows
    would cascade-delete their extracted definitions and named-act cross-references, hence this
    backfill. Headings derive from the ltree segments (cast_10 becomes "Část 10") via
    graph.browse._struct_label, the labels browse already shows, so no source re-parse is needed.

    Invariants (checked by verification SQL and tests): idempotent (a re-run inserts and repoints
    nothing); every anchor has is_anchor_only, a resolved parent_id, valid_from and a label; no
    anchor is returned by the seed candidate query; walking up from any leaf reaches the act's root
    anchor; reversible with `unmake`; the d1_check, n5_check and n6_check integrity checks still
    pass.

    Afterwards, re-run the graph build (materialize_layer_a writes parent_of / child_of edges along
    the new parent_id chain) and reindex_intelligence. Anchors get no centrality of their own (only
    low-weight hierarchy edges touch them) and do not appear in the browse spine or regime views,
    which filter on graph membership; they exist purely to carry the trust tag.
    """
    pool = await get_pool()
    # Ensure the columns exist for both apply and dry run: the planner reads is_anchor_only to skip
    # existing anchors, so a dry run after an apply correctly reports no new work.
    await ensure_schema()
    async with pool.acquire() as conn:
        docs = [r["id"] for r in await conn.fetch("SELECT id::text id FROM source_document ORDER BY external_id")]
        totals = {"acts": 0, "anchors_planned": 0, "anchors_written": 0, "leaves_repointed": 0}
        for doc_id in docs:
            async with conn.transaction():
                r = await _backfill_act(conn, doc_id, apply=apply)
            if r["anchors_planned"]:
                totals["acts"] += 1
                for k in ("anchors_planned", "anchors_written", "leaves_repointed"):
                    totals[k] += r[k]
    totals["mode"] = "APPLIED" if apply else "DRY-RUN (no write)"
    return totals


async def unmake(*, drop_schema: bool = False) -> dict:
    """Remove all anchors: reset every repointed leaf's parent_id to NULL, then delete the anchor rows.

    The next materialize_layer_a rebuilds hierarchy edges from parent_id and so drops the parent_of /
    child_of edges along the deleted chain; any edge touching an anchor is also deleted here, so the
    graph is clean even before re-materialising.

    The is_anchor_only and anchor_level columns are kept by default, because the seed candidate query
    (graph.seed), the four_corners walk (graph.cte_expand) and the browse projections reference them.
    With every row false, those predicates change nothing. Pass drop_schema=True
    (CLI: --unmake --drop-schema) to remove the columns too; that is only safe once those queries no
    longer reference them.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Leaves whose parent_id points at an anchor go back to NULL (the inverse of the repoint).
            restored = await conn.fetchval(
                """WITH up AS (
                       UPDATE provision c SET parent_id = NULL
                        FROM provision a
                       WHERE c.parent_id = a.id AND a.is_anchor_only = true
                       RETURNING c.id)
                   SELECT count(*) FROM up"""
            )
            # Drop any edge touching an anchor (defensive; materialisation would also rebuild these).
            await conn.execute(
                """DELETE FROM pythia_edge e USING provision a
                    WHERE a.is_anchor_only = true AND (e.src = a.id OR e.dst = a.id)"""
            )
            deleted = await conn.fetchval(
                "WITH d AS (DELETE FROM provision WHERE is_anchor_only = true RETURNING 1) SELECT count(*) FROM d"
            )
            if drop_schema:
                await conn.execute(
                    "DROP INDEX IF EXISTS ix_provision_anchor_only;"
                    "ALTER TABLE provision DROP COLUMN IF EXISTS is_anchor_only;"
                    "ALTER TABLE provision DROP COLUMN IF EXISTS anchor_level;"
                )
    return {"leaves_restored": restored, "anchors_deleted": deleted, "schema_dropped": drop_schema}


async def _main() -> None:
    if "--ensure-schema" in sys.argv:
        await ensure_schema()
        print("✅ ensured: provision.is_anchor_only + provision.anchor_level + ix_provision_anchor_only.")
        await close_pool()
        return
    if any(f in sys.argv for f in ("--unmake", "--drop", "--down")):
        res = await unmake(drop_schema="--drop-schema" in sys.argv)
        tail = (
            "; dropped is_anchor_only/anchor_level columns"
            if res["schema_dropped"]
            else "; kept the is_anchor_only column seam (all-false no-op)"
        )
        print(
            f"⏪ reverted: restored {res['leaves_restored']:,} leaf parent_id → NULL; "
            f"deleted {res['anchors_deleted']:,} section anchors{tail}."
        )
        await close_pool()
        return
    res = await backfill(apply=("--backfill" in sys.argv))
    print(f"=== B1a-03 section anchors — {res['mode']} ===")
    print(f"  acts with structure        : {res['acts']:,}")
    print(f"  section anchors planned     : {res['anchors_planned']:,}")
    print(f"  section anchors written     : {res['anchors_written']:,}")
    print(f"  leaves repointed (CZ NULLs) : {res['leaves_repointed']:,}")
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --backfill to materialize; --unmake to revert)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
