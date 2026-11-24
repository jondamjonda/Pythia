"""NT4 durability — materialize_layer_a must PRESERVE externally-managed structural edges (DB integration).

Regression lock for the audit defect where materialize's blanket TRUNCATE wiped the text-derived nt4_repeal
edges on every run (they had to be hand-restored). materialize now scoped-deletes only the sources it owns
(cross_reference / hierarchy / defined_term / transposition), so externally-managed edges survive a rebuild."""

from __future__ import annotations

from pythia.db import get_pool
from pythia.graph.materialize import materialize_layer_a

_SENTINEL = "test_external_keepsake"


async def test_materialize_preserves_external_structural_edges():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM pythia_edge WHERE source=$1", _SENTINEL)
        two = await conn.fetch("SELECT id FROM provision ORDER BY id LIMIT 2")
        src, dst = two[0]["id"], two[1]["id"]
        # a non-self-loop edge under a source materialize does NOT own (mirrors nt4_repeal)
        await conn.execute(
            "INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source) VALUES ($1,$2,15,'repeals',0.4,$3)",
            src,
            dst,
            _SENTINEL,
        )
        assert await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE source=$1", _SENTINEL) == 1
    try:
        await materialize_layer_a()  # the op that used to TRUNCATE the whole table
        async with pool.acquire() as conn:
            survived = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE source=$1", _SENTINEL)
            owned = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE source='cross_reference'")
        assert survived == 1  # the externally-managed edge is preserved
        assert owned > 0  # and materialize still rebuilt its own edges
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM pythia_edge WHERE source=$1", _SENTINEL)
