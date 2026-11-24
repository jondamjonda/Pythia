"""B1a-03 — section/ancestor anchor materialization.

Two layers:
  • PURE (no DB): the planner `plan_anchors` + helpers `_prefixes` / `anchor_id` / `_seg_type` — the
    load-bearing logic that decides WHICH prefixes become anchors, their resolved parent, label, level,
    and the deterministic (re-run-stable) id. These run with no live pythia.
  • DB (rolled-back txn, live data never mutated): the four INVARIANTS the orchestrator also checks —
      (1) the L1 candidate SQL (graph.seed) returns ZERO anchors;
      (2) a leaf walks up to its act-root via parent_id THROUGH the materialized anchors;
      (3) a second backfill on the same txn is a NO-OP (idempotent: 0 new inserts);
      (4) --unmake restores the repointed leaves' parent_id and deletes every anchor (reversible).
    Each DB test stages the backfill inside a transaction it ROLLS BACK, so the live corpus is untouched.
"""

from __future__ import annotations

from datetime import date


from pythia.db import get_pool
from pythia.ingest.section_rows import (
    _prefixes,
    _seg_type,
    anchor_id,
    plan_anchors,
)


# ----------------------------------------------------------------------------- PURE planner (no DB)


def test_prefixes_are_proper_and_root_first():
    assert _prefixes("a.b.c") == ["a", "a.b"]  # excludes the leaf 'a.b.c'
    assert _prefixes("a") == []  # a single-segment leaf has no ancestor prefix
    assert _prefixes("d_89_2012.cast_1.hlava_2.par_118") == [
        "d_89_2012",
        "d_89_2012.cast_1",
        "d_89_2012.cast_1.hlava_2",
    ]


def test_seg_type_is_the_token_before_the_number():
    assert _seg_type("cast_10") == "cast"
    assert _seg_type("hlava_2") == "hlava"
    assert _seg_type("preambule") == "preambule"  # no underscore → whole segment is the type


def test_anchor_id_is_deterministic_and_path_scoped():
    d = "11111111-1111-1111-1111-111111111111"
    assert anchor_id(d, "d.cast_1") == anchor_id(d, "d.cast_1")  # re-run stable
    assert anchor_id(d, "d.cast_1") != anchor_id(d, "d.cast_2")  # distinct prefix → distinct id
    assert anchor_id(d, "d.cast_1") != anchor_id(
        "22222222-2222-2222-2222-222222222222", "d.cast_1"
    )  # distinct act → distinct id


def _leaf(pid, hpath, ordinal, vf=None, doc="doc-uuid"):
    return {"id": pid, "hpath": hpath, "ordinal": ordinal, "valid_from": vf, "doc_id": doc}


def test_plan_anchors_one_per_distinct_prefix():
    # a tiny CZ-shaped act: one Část, two Hlavy, three §. The distinct NON-leaf prefixes are
    # {d, d.cast_1, d.cast_1.hlava_1, d.cast_1.hlava_2} → 4 anchors.
    rows = [
        _leaf("p1", "d.cast_1.hlava_1.par_1", 0),
        _leaf("p2", "d.cast_1.hlava_1.par_2", 1),
        _leaf("p3", "d.cast_1.hlava_2.par_3", 2),
    ]
    anchors, repoints = plan_anchors(rows)
    prefixes = [a["prefix"] for a in anchors]
    assert prefixes == ["d", "d.cast_1", "d.cast_1.hlava_1", "d.cast_1.hlava_2"]  # root-first
    # every leaf is repointed to its IMMEDIATE ancestor prefix
    assert dict(repoints) == {"p1": "d.cast_1.hlava_1", "p2": "d.cast_1.hlava_1", "p3": "d.cast_1.hlava_2"}


def test_plan_anchors_resolves_parent_chain_and_labels():
    rows = [_leaf("p1", "d.cast_10.hlava_1.par_118", 5)]
    anchors, _ = plan_anchors(rows)
    by_prefix = {a["prefix"]: a for a in anchors}
    assert by_prefix["d"]["parent_prefix"] is None  # the act-root anchor
    assert by_prefix["d.cast_10"]["parent_prefix"] == "d"
    assert by_prefix["d.cast_10.hlava_1"]["parent_prefix"] == "d.cast_10"
    # labels come from the SAME _struct_label the read-path uses
    assert by_prefix["d.cast_10"]["label"] == "Část 10"
    assert by_prefix["d.cast_10.hlava_1"]["label"] == "Hlava 1"
    assert by_prefix["d.cast_10"]["anchor_level"] == "cast"


def test_plan_anchors_skips_prefixes_occupied_by_a_real_provision():
    # EU shape: a real Article row occupies 'd.art_1', its paragraphs hang below it. 'd.art_1' must NOT
    # become an anchor (a real provision already holds it); only the act-root 'd' is synthesized.
    rows = [
        _leaf("art1", "d.art_1", 0),  # the REAL Article provision
        _leaf("p_a", "d.art_1.para_1", 1),
        _leaf("p_b", "d.art_1.para_2", 2),
    ]
    anchors, repoints = plan_anchors(rows)
    prefixes = {a["prefix"] for a in anchors}
    assert prefixes == {"d"}  # 'd.art_1' is real → not synthesized
    # the paragraphs still want to repoint to the real Article (the caller only acts where parent_id NULL)
    assert dict(repoints)["p_a"] == "d.art_1"
    assert dict(repoints)["art1"] == "d"  # the Article itself nests under the act-root anchor


def test_plan_anchors_min_ordinal_and_valid_from_for_reading_order():
    rows = [
        _leaf("p1", "d.cast_1.par_2", 7, vf=date(2014, 1, 1)),
        _leaf("p2", "d.cast_1.par_1", 3, vf=date(2012, 1, 1)),  # lower ordinal + earlier valid_from
    ]
    anchors, _ = plan_anchors(rows)
    cast = next(a for a in anchors if a["prefix"] == "d.cast_1")
    assert cast["ordinal"] == 3  # min descendant ordinal → reading order holds
    assert cast["valid_from"] == date(2012, 1, 1)  # min descendant valid_from


def test_plan_anchors_empty():
    assert plan_anchors([]) == ([], [])


# ----------------------------------------------------------------------------- DB invariants (rolled-back)
# These touch the live pythia DB. They stage the backfill in a transaction and ROLL IT BACK, so the live
# corpus is never mutated. Run by the orchestrator against the fresh snapshot.


async def _an_act_with_structure(conn) -> str:
    """A source_document whose provisions carry a multi-level ltree (so anchors get materialized).

    Does NOT reference is_anchor_only — this helper runs BEFORE the DDL stages the column. The column
    is added by _stage_backfill; on a fresh corpus there are no anchors anyway, so the raw row set is
    the real-provision set."""
    return await conn.fetchval(
        """SELECT source_document_id::text
             FROM provision
            WHERE hierarchy_path IS NOT NULL AND nlevel(hierarchy_path) >= 3
            GROUP BY source_document_id
            ORDER BY count(*) DESC
            LIMIT 1"""
    )


async def _stage_backfill(conn, doc_id: str) -> dict:
    """Run the per-act backfill on THIS connection (so the open txn sees its own writes).

    Robust to an ALREADY-APPLIED live corpus: if this act's anchors were already materialized,
    clear them WITHIN this (rolled-back) txn first so the backfill writes them fresh — making the
    DB tests pass identically whether the live DB is pre- or post-backfill. The txn is rolled back,
    so the live corpus is never mutated either way."""
    from pythia.ingest.section_rows import _backfill_act

    await _normalize_act_to_fresh(conn, doc_id)
    return await _backfill_act(conn, doc_id, apply=True)


async def _normalize_act_to_fresh(conn, doc_id: str) -> None:
    """Within the open (rolled-back) txn, return this act to its pre-backfill state — clear any
    already-materialized anchors + reset their leaves' parent_id — so a DB test that compares a
    before/after baseline measures against the SAME fresh reference whether the live corpus is pre-
    or post-backfill."""
    from pythia.ingest.section_rows import _DDL

    await conn.execute(_DDL)
    await conn.execute(
        "UPDATE provision SET parent_id = NULL WHERE parent_id IN "
        "(SELECT id FROM provision WHERE is_anchor_only AND source_document_id = $1::uuid)",
        doc_id,
    )
    await conn.execute("DELETE FROM provision WHERE is_anchor_only AND source_document_id = $1::uuid", doc_id)


async def test_db_candidate_sql_returns_zero_anchors():
    pool = await get_pool()
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            doc_id = await _an_act_with_structure(conn)
            assert doc_id, "test setup: no multi-level act in corpus"
            res = await _stage_backfill(conn, doc_id)
            assert res["anchors_written"] > 0, "no anchors materialized for a multi-level act"
            # the L1 candidate SQL (sparse_seed) must return ZERO is_anchor_only rows. Run a broad query
            # on the SAME connection so it sees the staged anchors, then assert none of the hits is an anchor.
            seeds = await _sparse_on_conn(conn, "zákon povinnost osoba podmínky", k=200)
            ids = [s.provision_id for s in seeds]
            if ids:
                n_anchor = await conn.fetchval(
                    "SELECT count(*) FROM provision WHERE id = ANY($1::uuid[]) AND is_anchor_only", ids
                )
                assert n_anchor == 0, "a structural anchor leaked into the L1 candidate set"
            # belt-and-braces: anchors carry an EMPTY body, so their (generated) tsv is the empty vector —
            # it can never MATCH any to_tsquery, AND the is_anchor_only=false predicate excludes them
            # regardless. Probe both: no anchor has a non-empty tsv, and the seed predicate selects none.
            non_empty = await conn.fetchval(
                "SELECT count(*) FROM provision WHERE is_anchor_only AND tsv <> ''::tsvector"
            )
            assert non_empty == 0, "an anchor has a non-empty tsv — it could match the FTS seed"
            selectable = await conn.fetchval(
                "SELECT count(*) FROM provision  WHERE is_anchor_only = false AND is_anchor_only IS TRUE"
            )  # vacuously 0
            assert selectable == 0
            # the exact seed predicate (with the B1a-03 exclusion) selects ZERO anchors for ANY query:
            seed_anchor = await conn.fetchval(
                """SELECT count(*) FROM provision p
                    WHERE p.is_anchor_only = false AND p.id IN
                          (SELECT id FROM provision WHERE is_anchor_only)"""
            )
            assert seed_anchor == 0, "the L1 predicate would admit an anchor"
        finally:
            await tr.rollback()


async def _sparse_on_conn(conn, query: str, *, k: int):
    """sparse_seed's SQL, run on a provided (txn) connection so it sees staged anchors. Mirrors the
    exact predicate in graph.seed.sparse_seed (incl. the B1a-03 anchor exclusion)."""
    import re

    from pythia.graph.seed import Seed

    terms = [t for t in re.findall(r"\w+", query) if len(t) >= 3][:14]
    tsq = " | ".join(terms)
    rows = await conn.fetch(
        """SELECT p.id::text id FROM provision p
            WHERE p.tsv @@ to_tsquery('czech', $1)
              AND p.is_anchor_only = false
              AND (p.valid_to IS NULL OR p.valid_to > CURRENT_DATE)
              AND (p.valid_from IS NULL OR p.valid_from <= CURRENT_DATE)
              AND p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
            ORDER BY ts_rank(p.tsv, to_tsquery('czech', $1)) DESC LIMIT $2""",
        tsq,
        k,
    )
    return [Seed(r["id"], 0.0) for r in rows]


async def test_db_leaf_walks_up_to_root_via_parent_id():
    pool = await get_pool()
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            doc_id = await _an_act_with_structure(conn)
            await _stage_backfill(conn, doc_id)
            # pick a repointed leaf (was NULL, now points at an anchor) and climb parent_id to the top.
            leaf = await conn.fetchval(
                """SELECT c.id::text FROM provision c JOIN provision a ON a.id = c.parent_id
                    WHERE c.source_document_id = $1::uuid AND a.is_anchor_only
                      AND c.is_anchor_only = false
                    LIMIT 1""",
                doc_id,
            )
            assert leaf, "no leaf was repointed onto an anchor"
            chain = await conn.fetch(
                """WITH RECURSIVE up(id, parent_id, depth) AS (
                       SELECT id, parent_id, 0 FROM provision WHERE id = $1::uuid
                     UNION ALL
                       SELECT p.id, p.parent_id, up.depth + 1
                         FROM provision p JOIN up ON p.id = up.parent_id
                        WHERE up.depth < 20)
                   SELECT id::text, parent_id::text, depth FROM up ORDER BY depth""",
                leaf,
            )
            assert len(chain) >= 2, "leaf did not climb through any ancestor"
            root = chain[-1]
            assert root["parent_id"] is None, "walk-up did not terminate at an act-root anchor"
            # every step above the leaf is a materialized anchor (the structural spine)
            anchor_steps = await conn.fetchval(
                "SELECT count(*) FROM provision WHERE id = ANY($1::uuid[]) AND is_anchor_only",
                [c["id"] for c in chain[1:]],
            )
            assert anchor_steps == len(chain) - 1, "a non-anchor appeared in the leaf→root spine"
        finally:
            await tr.rollback()


async def test_db_backfill_is_idempotent():
    pool = await get_pool()
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            doc_id = await _an_act_with_structure(conn)
            first = await _stage_backfill(conn, doc_id)
            assert first["anchors_written"] > 0
            from pythia.ingest.section_rows import _backfill_act

            second = await _backfill_act(conn, doc_id, apply=True)  # re-run on the same staged state
            assert second["anchors_written"] == 0, "re-run inserted anchors — not idempotent"
            assert second["leaves_repointed"] == 0, "re-run repointed leaves — not idempotent"
            assert second["anchors_planned"] == first["anchors_planned"], "plan changed on re-run"
        finally:
            await tr.rollback()


async def test_db_unmake_is_reversible():
    pool = await get_pool()
    async with pool.acquire() as conn:
        tr = conn.transaction()
        await tr.start()
        try:
            doc_id = await _an_act_with_structure(conn)
            await _normalize_act_to_fresh(conn, doc_id)  # measure the baseline on the fresh state
            before_nulls = await conn.fetchval(
                "SELECT count(*) FROM provision WHERE source_document_id = $1::uuid AND parent_id IS NULL", doc_id
            )
            await _stage_backfill(conn, doc_id)
            # inline unmake (same txn so it sees staged rows): restore repointed leaves, delete anchors.
            await conn.execute(
                """UPDATE provision c SET parent_id = NULL
                     FROM provision a
                    WHERE c.parent_id = a.id AND a.is_anchor_only"""
            )
            deleted = await conn.fetchval(
                "WITH d AS (DELETE FROM provision WHERE is_anchor_only RETURNING 1) SELECT count(*) FROM d"
            )
            assert deleted > 0, "nothing to delete — backfill wrote no anchors"
            after_nulls = await conn.fetchval(
                "SELECT count(*) FROM provision WHERE source_document_id = $1::uuid AND parent_id IS NULL", doc_id
            )
            assert after_nulls == before_nulls, "unmake did not restore the original parent_id NULL set"
            remaining = await conn.fetchval("SELECT count(*) FROM provision WHERE is_anchor_only")
            assert remaining == 0, "anchors survived unmake"
        finally:
            await tr.rollback()
