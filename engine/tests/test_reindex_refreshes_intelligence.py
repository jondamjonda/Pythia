"""N6 S5 — durability wiring: refresh._reindex must refresh the intelligence layer (DB integration).

The defect this locks: build_intelligence was in NO ingest/reindex path, so the navigation layer rotted
stale (the 67.5%-stale bug) as the corpus grew. S5 wires reindex_intelligence() as the FINAL graph-mutating
step of _reindex (after apply_structural + the backfills) and folds n6_check into the post-reindex self-verify.

We prove:
  • STAGING — a previously-isolated (NULL centrality), in-force provision that gains a traversable in-force
    edge becomes scored (non-NULL centrality + community) after _reindex runs.
  • ORDERING — reindex_intelligence runs AFTER apply_structural (load-bearing: PageRank/Louvain must read the
    fully-materialized edge set).
  • FRESHNESS — n6_check ok=True at the end of _reindex (asserted in _reindex's own return + re-checked here).
  • SHORT-CIRCUIT — a no-delta second _reindex's intelligence step is a near no-op (fingerprint match), well
    within the S1 perf budget.

Teardown removes the staging edge and force-rebuilds so the live layer returns to its real (17,108-node) state.
"""

from __future__ import annotations

import time

import pytest

import pythia.graph.intelligence as intelligence
import pythia.ingest.structural_edges as structural_edges
from pythia.db import get_pool
from pythia.graph.intelligence import (
    build_intelligence,
    directed_graph,
    load_current_law_edges,
    reindex_intelligence,
)
from pythia.graph.ppr import clear_ppr_cache
from pythia.ingest.n6_check import check as n6_check
from pythia.ingest.refresh import _reindex

_SENTINEL = "test_n6_durability_edge"


async def _isolated_in_force_provision(conn, node_set: set[str]) -> str:
    """An in-force, non-CASE_LAW provision that is NOT currently a scored node (genuinely isolated).

    DETERMINISTIC + complete scan (ORDER BY id, no LIMIT): isolated provisions are a minority of the
    table, so the previous unordered `LIMIT 4000` window could — under a different live `provision` heap
    order left by sibling DB-mutating tests — contain only node-set members and raise spuriously. A full
    id-ordered scan always finds the same isolated § regardless of physical row order."""
    rows = await conn.fetch(
        """SELECT p.id::text AS id FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE (p.valid_to IS NULL OR p.valid_to > CURRENT_DATE)
              AND sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
            ORDER BY p.id"""
    )
    for r in rows:
        if r["id"] not in node_set:
            return r["id"]
    raise AssertionError("no isolated in-force provision available to stage")


async def _scored_in_force_target(conn) -> str:
    # ORDER BY id: a deterministic, heap-order-independent pick (a scored node ⇒ in the current node-set,
    # so the `nodes == len(node_set) + 1` assertion holds: the only new node is the staged isolated one).
    return await conn.fetchval(
        "SELECT id::text FROM provision WHERE centrality IS NOT NULL "
        "AND (valid_to IS NULL OR valid_to > CURRENT_DATE) ORDER BY id LIMIT 1"
    )


async def _cleanup(conn) -> None:
    await conn.execute("DELETE FROM pythia_edge WHERE source = $1", _SENTINEL)


@pytest.mark.live
async def test_reindex_scores_a_newly_edged_provision_and_orders_after_structural(monkeypatch):
    pool = await get_pool()
    # Drop any leftover sentinel edge from a prior crashed run BEFORE the baseline read — a leaked sentinel
    # would otherwise pull its endpoints into `node_set` and then leave it again at reindex, breaking the
    # exact `nodes == len(node_set) + 1` count. (Ordering hygiene: clean first, then measure.)
    async with pool.acquire() as conn:
        await _cleanup(conn)

    # Baseline node-set (post-cleanup) so we can pick a provision that is genuinely isolated right now.
    edges = await load_current_law_edges()
    node_set = set(directed_graph(edges).ids)

    async with pool.acquire() as conn:
        iso = await _isolated_in_force_provision(conn, node_set)
        tgt = await _scored_in_force_target(conn)
        # Sanity: the staged provision really is NULL on both axes before we wire it in.
        pre = await conn.fetchrow("SELECT centrality, community FROM provision WHERE id = $1::uuid", iso)
        assert pre["centrality"] is None and pre["community"] is None
        # A traversable (rel 1 'references', weight>0), in-force, non-self edge under a sentinel source
        # materialize does NOT own (so it survives the materialize rebuild inside _reindex), pulling the
        # isolated provision into the current-law node-set.
        await conn.execute(
            "INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source) "
            "VALUES ($1::uuid, $2::uuid, 1, 'references', 0.9, $3) ON CONFLICT DO NOTHING",
            iso,
            tgt,
            _SENTINEL,
        )

    # ORDERING SPY — record the order apply_structural and reindex_intelligence are invoked in. _reindex
    # imports them by name from these modules, so patching the module attribute intercepts the call.
    order: list[str] = []
    real_apply_structural = structural_edges.apply_structural
    real_reindex_intelligence = intelligence.reindex_intelligence

    async def spy_apply_structural(*a, **k):
        order.append("apply_structural")
        return await real_apply_structural(*a, **k)

    async def spy_reindex_intelligence(*a, **k):
        order.append("reindex_intelligence")
        return await real_reindex_intelligence(*a, **k)

    monkeypatch.setattr(structural_edges, "apply_structural", spy_apply_structural)
    monkeypatch.setattr(intelligence, "reindex_intelligence", spy_reindex_intelligence)

    try:
        res = await _reindex()

        # ORDERING — intelligence ran, and ran AFTER apply_structural.
        assert "apply_structural" in order and "reindex_intelligence" in order
        assert order.index("reindex_intelligence") > order.index("apply_structural")

        # FRESHNESS — _reindex self-verified N6 (and N5) green in the same pass.
        assert res["n6_ok"] is True, res
        assert res["n5_ok"] is True, res
        assert res["invariants_ok"] is True

        # STAGING — the previously-isolated provision is now scored on BOTH axes.
        async with pool.acquire() as conn:
            post = await conn.fetchrow("SELECT centrality, community FROM provision WHERE id = $1::uuid", iso)
        assert post["centrality"] is not None, "newly-edged provision must gain centrality"
        assert post["community"] is not None, "newly-edged provision must gain community"

        # The intelligence build result is folded into the _reindex return dict.
        assert res["intelligence"]["nodes"] == len(node_set) + 1  # exactly the one new node
        assert res["intelligence"]["edge_count"] is not None

        # n6_check confirms freshness independently at the end.
        assert (await n6_check())["ok"] is True
    finally:
        # SELF-CLEAN (suite determinism): this test runs a FULL live `_reindex` — it stages a sentinel edge
        # in `pythia_edge`, recomputes centrality/community on the live `provision` table, and (via
        # reindex_intelligence) busts the module-global in-RAM PPR default-view cache. Teardown must undo
        # ALL THREE so siblings see the true corpus, else the byte-stable browse goldens / spine / regime
        # tests read a sentinel-perturbed graph and fail only in the full suite.
        async with pool.acquire() as conn:
            await _cleanup(conn)  # (1) drop the sentinel edge
        # (2) restore the persisted layer over the now-baseline edge set via reindex_intelligence (NOT bare
        # build_intelligence): it is the one funnel that ALSO busts the in-RAM PPR cache, so the cache can
        # never carry a graph computed mid-perturbation into the next test (the A→B→A oscillation the cache
        # comment warns about). force=True bypasses the freshness short-circuit so the restore always writes.
        await reindex_intelligence(force=True)
        clear_ppr_cache()  # (3) belt-and-suspenders RAM-cache bust

    # Post-restore the staged node is isolated again → NULL on both axes.
    async with pool.acquire() as conn:
        restored = await conn.fetchrow("SELECT centrality, community FROM provision WHERE id = $1::uuid", iso)
    assert restored["centrality"] is None and restored["community"] is None


@pytest.mark.live
async def test_no_delta_reindex_intelligence_short_circuits_fast():
    # Make the layer + fingerprint fresh first.
    await build_intelligence(apply=True, force=True)
    # A no-delta intelligence refresh (the hot path inside a no-op _reindex) must short-circuit: no
    # recompute, no write — comfortably within the S1 sub-second write budget (we allow generous slack
    # for the cheap aggregate count + fingerprint read).
    t0 = time.perf_counter()
    res = await reindex_intelligence()
    dt = time.perf_counter() - t0
    assert res["mode"].startswith("SKIPPED"), res["mode"]
    assert dt < 5.0, f"fingerprint short-circuit took {dt:.2f}s — expected near-instant"
