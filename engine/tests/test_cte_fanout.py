"""four_corners fan-out cap (task 3) — deterministic proofs of the per-node top-N-by-weight cap.

A recursive CTE costs O(branch_factor ^ max_hops): a definition hub (~380 out-edges) blows the walk
past the latency budget. The cap bounds each node's expansion to its top-N highest-weight out-edges.
Two guarantees are pinned here, both with synthetic edges staged in a ROLLED-BACK transaction:

  • the RECURSIVE step caps to exactly fanout_cap (its top-N by weight), and is a no-op above degree;
  • the SEED step uses GREATEST(fanout_cap, k), so depth-1 top-k nodes (whose score == their seed-edge
    weight) are NEVER dropped even when k > fanout_cap.
"""

from __future__ import annotations

from pythia.db import close_pool, get_pool
from pythia.graph.cte_expand import four_corners


# A RETURNABLE answer-node predicate (mirrors the final SELECT of _FOUR_CORNERS_SQL): a node the walk may
# actually emit. B1a-03 materialized ~hundreds of `is_anchor_only` section anchors per code; the four-corners
# result filters them out (`p.is_anchor_only = false`) and also masks anything not in force TODAY
# (valid_to open/future AND valid_from past). Staging edges onto an anchor (or a future-effective) target
# would therefore stage onto a node the cap traverses-through but NEVER returns — so the top-N-by-weight
# assertions must seed only returnable targets to stay a clean proof of the cap. Distinct weights + this
# predicate ⇒ the top-N is unambiguous (no anchor row sneaking a weight-tie at the cap boundary).
_RETURNABLE = """legal_hierarchy = 'STATUTE' AND is_anchor_only = false AND valid_to IS NULL
              AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)"""


async def _leaf_statutes(conn, n: int) -> list[str]:
    """n RETURNABLE STATUTE provisions (non-anchor, in force today) with NO existing traversable out-edges,
    so their fan-out is ONLY what we stage (isolates the cap). With 17k provisions there are many such
    leaves (248 on the current corpus)."""
    return [
        r["id"]
        for r in await conn.fetch(
            f"""SELECT p.id::text AS id FROM provision p
            WHERE {_RETURNABLE}
              AND NOT EXISTS (SELECT 1 FROM pythia_edge e WHERE e.src = p.id AND e.dst IS NOT NULL)
            LIMIT $1""",
            n,
        )
    ]


async def _stage_edge(conn, src: str, dst: str, weight: float) -> None:
    await conn.execute(
        """INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source)
           VALUES ($1::uuid, $2::uuid, 1, 'references', $3, 'test_fanout')""",
        src,
        dst,
        weight,
    )


async def test_recursive_step_caps_to_top_n_by_weight():
    """RECURSIVE-step cap (always = fanout_cap): a 2-hop S→H→{10 targets}; capping H's expansion to N
    keeps exactly its N highest-weight edges (and a cap ≥ degree is a no-op)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        leaves = await _leaf_statutes(conn, 2)
        assert len(leaves) == 2, "test setup: need two leaf STATUTE seeds"
        s, h = leaves
        targets = [
            r["id"]
            for r in await conn.fetch(
                f"""SELECT id::text AS id FROM provision
                WHERE {_RETURNABLE} AND id <> ALL($1::uuid[])
                LIMIT 10""",
                [s, h],
            )
        ]
        assert len(targets) == 10

        tx = conn.transaction()
        await tx.start()
        try:
            await _stage_edge(conn, s, h, 1.0)  # S → H (the only depth-1 edge)
            for i, t in enumerate(targets):  # H → 10 targets, distinct weights
                await _stage_edge(conn, h, t, round((i + 1) / 10, 3))
            top4 = set(targets[6:])  # weights 0.7, 0.8, 0.9, 1.0
            bottom6 = set(targets[:6])  # weights 0.1 … 0.6 — cut at cap=4

            cap4 = {x.provision_id for x in await four_corners([s], max_hops=2, k=100, fanout_cap=4, conn=conn)}
            assert top4 <= cap4, "recursive cap dropped one of the four highest-weight edges"
            assert not (bottom6 & cap4), "recursive cap kept a below-threshold (lower-weight) edge"
            assert len(cap4 & set(targets)) == 4, (
                f"cap=4 must reach exactly 4 targets, reached {len(cap4 & set(targets))}"
            )

            cap_all = {x.provision_id for x in await four_corners([s], max_hops=2, k=100, fanout_cap=10, conn=conn)}
            assert set(targets) <= cap_all, "cap >= degree must reach ALL edges (no-op under the cap)"
        finally:
            await tx.rollback()
    await close_pool()


async def test_seed_step_never_drops_top_k_when_cap_below_k():
    """SEED-step GREATEST(fanout_cap, k) guarantee: at depth 1 a node's score IS its seed-edge weight, so
    the true top-k depth-1 nodes are the k highest-weight seed edges; the seed step must keep them even
    when fanout_cap < k (a plain LIMIT fanout_cap would silently drop guaranteed-top-k results)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        leaves = await _leaf_statutes(conn, 1)
        assert leaves, "test setup: need a leaf STATUTE seed"
        seed = leaves[0]
        targets = [
            r["id"]
            for r in await conn.fetch(
                f"""SELECT id::text AS id FROM provision
                WHERE {_RETURNABLE} AND id <> $1::uuid
                LIMIT 10""",
                seed,
            )
        ]
        assert len(targets) == 10

        tx = conn.transaction()
        await tx.start()
        try:
            for i, t in enumerate(targets):
                await _stage_edge(conn, seed, t, round((i + 1) / 10, 3))
            # fanout_cap=4 < k=10 → GREATEST(4, 10)=10, so all 10 depth-1 nodes survive (no top-k loss).
            reached = {x.provision_id for x in await four_corners([seed], max_hops=1, k=10, fanout_cap=4, conn=conn)}
            assert set(targets) <= reached, (
                "seed step dropped a top-k depth-1 node when fanout_cap < k (GREATEST guard failed)"
            )
        finally:
            await tx.rollback()
    await close_pool()
