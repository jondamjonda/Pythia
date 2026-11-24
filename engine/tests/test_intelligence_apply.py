"""N6 S1+S2 — intelligence --apply write contract (DB integration) + edge-set policy (pure).

Locks the atomic / scoped / idempotent write rewritten in S1 and the edge-set policy amended in S2:

  • CONSISTENCY  — after --apply, centrality NOT NULL ⇔ community NOT NULL, and that count equals the
                   distinct node-set of the scored graph (load_current_law_edges → directed_graph.n).
  • IDEMPOTENCE  — a second immediate --apply writes 0 changed rows (the no-delta re-apply is the hot
                   path; it must touch nothing, not re-NULL-and-refill the whole table).
  • ISOLATED     — provisions with no in-force traversable non-self edge stay NULL (NULL-on-isolated).
  • REPRODUCIBLE — two builds → byte-identical community ids + identical centrality-ranked spine order
                   (Polarius renders both; they are externally-visible and must be deterministic).
  • EDGE POLICY  — pure: rel 27/28 (NON_TRAVERSABLE_REL_IDS) are excluded from _EDGE_SQL, sourced from
                   the registry (not hardcoded); case_law edges are KEPT IN (the deliberate divergence
                   from ppr.load_ppr_graph's include_case_law=False).
"""

from __future__ import annotations

from pythia.db import get_pool
from pythia.graph import intelligence
from pythia.graph.intelligence import (
    build_intelligence,
    directed_graph,
    load_current_law_edges,
)
from pythia.graph.relations import NON_TRAVERSABLE_REL_IDS


# --- pure: edge-set policy (no DB) -------------------------------------------------------------


def test_edge_sql_excludes_non_traversable_from_registry():
    # The NOT IN list is sourced from the registry tuple, NOT hardcoded 27,28 in this file.
    expected = "(" + ",".join(str(i) for i in NON_TRAVERSABLE_REL_IDS) + ")"
    assert intelligence._NON_TRAVERSABLE == expected
    assert f"e.rel_id NOT IN {expected}" in intelligence._EDGE_SQL
    # registry currently pins the annuls/annulled_by lineage pair
    assert set(NON_TRAVERSABLE_REL_IDS) == {27, 28}


def test_edge_sql_keeps_case_law_edges_in():
    # DELIBERATE divergence from ppr.load_ppr_graph: N6 keeps case_law edges (no legal_hierarchy /
    # CASE_LAW filter), so statutes gain importance from being construed/applied. Guard against a
    # future copy-paste of ppr's include_case_law gate sneaking a CASE_LAW exclusion into N6's read.
    assert "CASE_LAW" not in intelligence._EDGE_SQL
    assert "legal_hierarchy" not in intelligence._EDGE_SQL
    assert "include_case_law" not in intelligence._EDGE_SQL


# --- DB integration: the --apply write contract ------------------------------------------------


async def _stored_layer(conn):
    """Read the persisted layer as ({id: centrality}, {id: community}, spine-order id list)."""
    rows = await conn.fetch(
        "SELECT id::text AS id, centrality, community FROM provision "
        "WHERE centrality IS NOT NULL OR community IS NOT NULL"
    )
    cen = {r["id"]: r["centrality"] for r in rows if r["centrality"] is not None}
    com = {r["id"]: r["community"] for r in rows if r["community"] is not None}
    spine = [
        r["id"]
        for r in await conn.fetch(
            "SELECT id::text AS id FROM provision WHERE centrality IS NOT NULL ORDER BY centrality DESC, id LIMIT 50"
        )
    ]
    return cen, com, spine


async def test_apply_is_consistent_scoped_and_idempotent():
    # node-set the build will score = distinct endpoints of the current-law, traversable, self-loop-free graph
    edges = await load_current_law_edges()
    expected_n = directed_graph(edges).n

    await build_intelligence(apply=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        n_cen = await conn.fetchval("SELECT count(*) FROM provision WHERE centrality IS NOT NULL")
        n_com = await conn.fetchval("SELECT count(*) FROM provision WHERE community IS NOT NULL")
        # XOR rows: scored-on-one-axis-only must be ZERO (centrality NULL ⇔ community NULL)
        xor = await conn.fetchval("SELECT count(*) FROM provision WHERE (centrality IS NULL) <> (community IS NULL)")

    assert n_cen == n_com == expected_n  # consistent, and == the scored node-set (today 17,108)
    assert xor == 0  # never half-scored

    # IDEMPOTENCE — a second immediate apply must change nothing.
    async with pool.acquire() as conn:
        before = await _stored_layer(conn)
    await build_intelligence(apply=True)
    async with pool.acquire() as conn:
        after = await _stored_layer(conn)
    assert before == after  # no-delta re-apply left the layer byte-identical


async def test_isolated_provisions_stay_null():
    await build_intelligence(apply=True)
    edges = await load_current_law_edges()
    node_set = set(directed_graph(edges).ids)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # a provision that is NOT a scored node (no in-force traversable non-self edge) must be NULL
        rows = await conn.fetch("SELECT id::text AS id, centrality, community FROM provision")
    isolated = [r for r in rows if r["id"] not in node_set]
    assert isolated, "expected some genuinely isolated provisions (~6,983 live)"
    assert all(r["centrality"] is None and r["community"] is None for r in isolated)


async def test_reproducible_community_ids_and_spine_order():
    await build_intelligence(apply=True)
    pool = await get_pool()
    async with pool.acquire() as conn:
        cen1, com1, spine1 = await _stored_layer(conn)

    await build_intelligence(apply=True)
    async with pool.acquire() as conn:
        cen2, com2, spine2 = await _stored_layer(conn)

    assert com1 == com2  # identical community ids (externally-visible, Polarius renders them)
    assert spine1 == spine2  # identical centrality-ranked spine order
    assert cen1 == cen2  # identical centrality scores
