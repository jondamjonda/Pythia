"""Graph-intelligence integrity check: is the centrality/community layer consistent and fresh?

The navigation layer built by ``pythia.graph.intelligence`` (a centrality score and a community
id per provision) feeds the graph explorer, which must never read a half-built, inconsistent or
stale layer. The individual checks are described in ``check``.

Run (from engine/): .venv/bin/python -m pythia.ingest.n6_check  (prints PASS/FAIL, exits 0 or 1)
"""

from __future__ import annotations

import asyncio
import json
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.intelligence import (
    _EVENT_DDL,
    current_fingerprint,
    directed_graph,
    last_fingerprint,
    load_current_law_edges,
)


async def check() -> dict:
    """Run the checks and return {ok, checks, detail}; labels match the printed PASS/FAIL table.

    N6.1 consistency: the number of provisions with a centrality, the number with a community and
         the size of the live current-law node set (distinct endpoints of the traversable,
         self-loop-free edges whose ends are both in force) are all equal, so the layer covers
         exactly the graph.
    N6.2 paired nullness: a provision is scored on both axes or on neither, never half-scored.
    N6.3 every endpoint of an in-force edge is scored, and every provision without one is NULL on
         both.
    N6.4 more than one community exists (a single cluster means Louvain collapsed: a bad build).
    N6.5 freshness: the stored build fingerprint matches the live edge set. This catches a layer
         that was built once and never refreshed as the corpus grew.
    """
    # The node set the layer must cover: distinct endpoints of the current-law, traversable,
    # self-loop-free edges with both ends in force (exactly the graph the build reads).
    edges = await load_current_law_edges()
    g = directed_graph(edges)
    node_set = set(g.ids)
    live_node_count = g.n

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_EVENT_DDL)  # tolerate a never-refreshed checkout
        live = await current_fingerprint(conn)  # {edge_count, edge_hash}: a content fingerprint
        live_edges = live["edge_count"]
        fp = await last_fingerprint(conn)

        n_cen = await conn.fetchval("SELECT count(*) FROM provision WHERE centrality IS NOT NULL")
        n_com = await conn.fetchval("SELECT count(*) FROM provision WHERE community IS NOT NULL")
        xor = await conn.fetchval("SELECT count(*) FROM provision WHERE (centrality IS NULL) <> (community IS NULL)")
        n_distinct_comm = await conn.fetchval(
            "SELECT count(DISTINCT community) FROM provision WHERE community IS NOT NULL"
        )
        # Scored rows outside the live node set are leaks: provisions with no in-force edge that the
        # scoped reset should have set to NULL.
        scored_ids = {
            str(r["id"])
            for r in await conn.fetch("SELECT id FROM provision WHERE centrality IS NOT NULL OR community IS NOT NULL")
        }

    scored_not_a_node = len(scored_ids - node_set)  # scored but isolated (should be 0)
    node_not_scored = len(node_set - scored_ids)  # edged but unscored (should be 0)

    fp_edge_count = fp.get("edge_count") if fp else None
    fp_edge_hash = fp.get("edge_hash") if fp else None
    # Freshness compares the edge-set content hash, not just the count, so a count-preserving change
    # (a redirected citation or a weight change) cannot leave the layer stale while the check passes.
    fresh = fp is not None and fp_edge_hash == live["edge_hash"] and fp_edge_count == live_edges

    checks = {
        "N6.1 consistency (n_cen==n_com==live node-set)": n_cen == n_com == live_node_count,
        "N6.2 paired nullness (cen NULL <> com NULL = 0)": xor == 0,
        "N6.3 edged⇒scored & isolated⇒NULL (leaks=0)": scored_not_a_node == 0 and node_not_scored == 0,
        "N6.4 communities split (distinct community > 1)": (n_distinct_comm or 0) > 1,
        "N6.5 freshness (stored fingerprint edge-set hash == live)": fresh,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "detail": {
            "n_centrality": n_cen,
            "n_community": n_com,
            "live_node_count": live_node_count,
            "live_edge_count": live_edges,
            "fingerprint_edge_count": fp_edge_count,
            "live_edge_hash": live["edge_hash"],
            "fingerprint_edge_hash": fp_edge_hash,
            "xor_rows": xor,
            "distinct_communities": n_distinct_comm,
            "scored_but_isolated": scored_not_a_node,
            "edged_but_unscored": node_not_scored,
            "has_fingerprint": fp is not None,
            "fingerprint": fp,
        },
    }


async def _main() -> None:
    res = await check()
    print("=== N6 invariant harness (graph-intelligence freshness + consistency) ===")
    for name, ok in res["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    d = res["detail"]
    print(
        f"\n  layer: centrality={d['n_centrality']} community={d['n_community']} "
        f"node-set={d['live_node_count']} · communities={d['distinct_communities']}"
    )
    print(
        f"  freshness: live edge_count={d['live_edge_count']} · "
        f"fingerprint edge_count={d['fingerprint_edge_count']} "
        f"({'present' if d['has_fingerprint'] else 'MISSING — never built'})"
    )
    if not res["ok"]:
        print("\n  detail:", json.dumps(d, ensure_ascii=False, default=str))
    print(f"\n  {'✅ ALL N6 INVARIANTS HOLD' if res['ok'] else '❌ INVARIANT VIOLATION'}")
    await close_pool()
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    asyncio.run(_main())
