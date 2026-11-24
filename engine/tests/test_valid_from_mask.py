"""valid_from point-in-time lower bound (task 4) — a provision with a FUTURE účinnost (valid_from) is
not yet law and must NOT appear in an as-of-today walk, but MUST appear once as_of reaches its effective
date. The masks previously filtered valid_to ONLY (the upper bound), so a future-effective provision
leaked into current results. Both retrieval lanes (four_corners + PPR) are pinned here, with the change
staged in a ROLLED-BACK transaction so live data is never mutated.

The predicate mirrors valid_to's NULL-tolerance: NULL valid_from ⇒ treat as always-in-force (missing
data is never used to HIDE a provision), so only a KNOWN future effective date excludes.
"""

from __future__ import annotations

from datetime import date

from pythia.db import close_pool, get_pool
from pythia.graph.cte_expand import four_corners
from pythia.graph.ppr import load_ppr_graph

_FUTURE = date(2099, 1, 1)
_AFTER_FUTURE = date(2100, 1, 1)


async def _hub_seed(conn) -> str:
    """A high-out-degree statute seed, so the walk reaches a stable set of nodes."""
    return await conn.fetchval(
        """SELECT e.src::text FROM pythia_edge e JOIN provision pd ON pd.id = e.dst
            WHERE e.dst IS NOT NULL AND e.rel_id NOT IN (27, 28)
              AND pd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
            GROUP BY e.src ORDER BY count(*) DESC LIMIT 1"""
    )


async def test_four_corners_respects_future_valid_from():
    pool = await get_pool()
    async with pool.acquire() as conn:
        seed = await _hub_seed(conn)
        base = {h.provision_id for h in await four_corners([seed], max_hops=2, k=300, conn=conn)}
        assert base, "test setup: hub seed reached nothing"
        # a reached node with valid_to NULL (so a future as_of does not expire it) and currently in force
        target = await conn.fetchval(
            """SELECT id::text FROM provision
                WHERE id = ANY($1::uuid[]) AND valid_to IS NULL
                  AND (valid_from IS NULL OR valid_from <= CURRENT_DATE) LIMIT 1""",
            list(base),
        )
        assert target, "test setup: no reached node with an open valid_to"

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("UPDATE provision SET valid_from = $2 WHERE id = $1::uuid", target, _FUTURE)
            today = {h.provision_id for h in await four_corners([seed], max_hops=2, k=300, conn=conn)}
            assert target not in today, "future-effective provision still appears in an as-of-today walk"
            ahead = {
                h.provision_id for h in await four_corners([seed], max_hops=2, k=300, as_of=_AFTER_FUTURE, conn=conn)
            }
            assert target in ahead, "provision absent at/after its effective date (lower bound too strict)"
        finally:
            await tx.rollback()
    await close_pool()


async def test_ppr_graph_respects_future_valid_from():
    pool = await get_pool()
    async with pool.acquire() as conn:
        # a node that is an endpoint in the default PPR graph and has an open valid_to
        target = await conn.fetchval(
            """SELECT e.src::text FROM pythia_edge e
                 JOIN provision ps ON ps.id = e.src JOIN provision pd ON pd.id = e.dst
                WHERE e.dst IS NOT NULL AND e.src <> e.dst AND e.rel_id NOT IN (27, 28)
                  AND ps.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                  AND pd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                  AND ps.valid_to IS NULL AND (ps.valid_from IS NULL OR ps.valid_from <= CURRENT_DATE)
                LIMIT 1"""
        )
        assert target, "test setup: no open-valid_to PPR endpoint"

        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("UPDATE provision SET valid_from = $2 WHERE id = $1::uuid", target, _FUTURE)
            g_today = await load_ppr_graph(None, conn=conn)  # as_of = today
            assert target not in g_today.idx, "future-effective node still in the as-of-today PPR graph"
            g_ahead = await load_ppr_graph(_AFTER_FUTURE, conn=conn)  # as_of past the effective date
            assert target in g_ahead.idx, "node absent from the PPR graph at/after its effective date"
        finally:
            await tx.rollback()
    await close_pool()
