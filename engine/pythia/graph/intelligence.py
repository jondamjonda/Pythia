"""Structural graph analytics: provision centrality and regime communities.

Two precomputed, question-independent indices over the current-law citation, definition and
hierarchy graph:

- centrality: global PageRank over the directed graph, i.e. each provision's structural importance
  (definition and scope hubs rise to the top). Used as a ranking prior. Reuses `ppr.PPRGraph`, since
  PageRank is PPR with a uniform teleport vector instead of seeds.
- community: Louvain modularity clusters over the undirected weighted graph, i.e. legal "regimes"
  (capital markets, payments, AML, insolvency, ...). Used for navigation. Communities are relabelled
  by size (0 = largest) so ids are stable and interpretable.

Both are stored as additive columns on `provision` (`centrality real`, `community int`). The default
run is a dry-run preview; `--apply` writes only changed rows (idempotent) and `--down` reverts.

Run:  .venv/bin/python -m pythia.graph.intelligence [--apply] [--down]   (default = dry-run)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime

import networkx as nx
import numpy as np
import scipy.sparse as sp
from networkx.algorithms.community import louvain_communities

from pythia.db import close_pool, get_pool
from pythia.graph.ppr import PPRGraph, clear_ppr_cache
from pythia.graph.relations import NON_TRAVERSABLE_REL_IDS

# This graph deliberately differs from ppr.load_ppr_graph: it keeps case-law edges (retrieval drops
# CASE_LAW endpoints by default), so statutes that rulings construe or apply gain structural importance.
# It does exclude the non-traversable lineage relations (annuls/annulled_by, rel 27/28: weight-0 status
# facts about erga omnes derogation). They are never a real navigation hop and must not skew centrality
# or communities. The ids come from relations.NON_TRAVERSABLE_REL_IDS rather than being hardcoded.
_NON_TRAVERSABLE = "(" + ",".join(str(i) for i in NON_TRAVERSABLE_REL_IDS) + ")"

# Self-loops are excluded (`e.src <> e.dst`). Historically about 46% of internal_reference edges were
# self-references (the extractor matching a provision's own § number); they are PageRank rank traps (a
# node voting for itself) with no navigational value. This query is separate from the shared retrieval
# loader `ppr.load_ppr_graph`, which is left untouched.
_EDGE_SQL = f"""
SELECT e.src::text AS src, e.dst::text AS dst, e.weight::float8 AS w
  FROM pythia_edge e
  JOIN provision ps ON ps.id = e.src
  JOIN provision pd ON pd.id = e.dst
 WHERE e.dst IS NOT NULL
   AND e.src <> e.dst
   AND e.rel_id NOT IN {_NON_TRAVERSABLE}
   AND (ps.valid_to IS NULL OR ps.valid_to > CURRENT_DATE)
   AND (pd.valid_to IS NULL OR pd.valid_to > CURRENT_DATE)
   AND (ps.valid_from IS NULL OR ps.valid_from <= CURRENT_DATE)   -- in force NOW: both endpoints already
   AND (pd.valid_from IS NULL OR pd.valid_from <= CURRENT_DATE)   -- effective (a future-účinnost node is not yet law)
 ORDER BY e.src, e.dst, e.rel_id
"""

# Cheap freshness signal: a content hash of exactly the edge set _EDGE_SQL feeds the build, computed
# server-side without loading rows (one aggregate scan, no sort, since bit_xor is order-independent).
# The edge count alone is not enough: a count-preserving change (a net-zero delete plus insert, a
# redirected citation, a weight change) can still move the PageRank scores and the Louvain partition.
# The per-edge hash of src|dst|weight changes on any such edit, so the build is skipped only when the
# result genuinely cannot change.
_EDGE_FINGERPRINT_SQL = f"""
SELECT count(*) AS edge_count,
       COALESCE(bit_xor(hashtext(src || '|' || dst || '|' || w::text)), 0) AS edge_hash
  FROM ({_EDGE_SQL}) _e
"""

_DDL = """
ALTER TABLE provision ADD COLUMN IF NOT EXISTS centrality real;
ALTER TABLE provision ADD COLUMN IF NOT EXISTS community int;
CREATE INDEX IF NOT EXISTS ix_p_centrality ON provision (centrality DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS ix_p_community ON provision (community);
"""
_DOWN = """
DROP INDEX IF EXISTS ix_p_centrality;
DROP INDEX IF EXISTS ix_p_community;
ALTER TABLE provision DROP COLUMN IF EXISTS centrality;
ALTER TABLE provision DROP COLUMN IF EXISTS community;
"""

_SEED = 42  # fixed Louvain RNG seed, so the partition (and the stored community ids) is reproducible

# Postgres advisory-lock key that serialises the reindex (read, compute, write). Every ingest
# finalisation calls reindex_intelligence, so concurrent ingestion means concurrent reindexes. Without
# the lock they interleave: one writes a partition computed from edge snapshot A while the other commits
# fingerprint B, leaving fingerprint and partition inconsistent (this made pythia.ingest.n6_check
# flaky). Held for the whole build and released on exit.
_REINDEX_LOCK_KEY = 0x6E365F7278  # ASCII "n6_rx"; stable and fits a signed bigint

# The build fingerprint is stored as a nexus_event of this kind, inside the same transaction as the
# data write, so a half-written layer never carries a "fresh" fingerprint. It reuses the event table
# refresh.py already provides instead of adding a separate run table.
_BUILD_EVENT_KIND = "intelligence-built"

# nexus_event is normally created by refresh._DDL, but build_intelligence can run before refresh ever
# has (a fresh checkout that ingests, then builds), so make sure the table exists first.
_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS nexus_event (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind text NOT NULL, payload jsonb, created_at timestamptz NOT NULL DEFAULT now(),
    consumed_at timestamptz);
CREATE INDEX IF NOT EXISTS ix_event_unconsumed ON nexus_event (created_at) WHERE consumed_at IS NULL;
"""


async def current_fingerprint(conn) -> dict:
    """Return the live ``{edge_count, edge_hash}`` of the edge set the build reads.

    Uses the same predicate as _EDGE_SQL, aggregated without loading rows. edge_hash is the content
    fingerprint compared by the build short-circuit and by n6_check; it changes on any edge-set edit
    (add, remove, rewire, reweight), not only when the count changes.
    """
    r = await conn.fetchrow(_EDGE_FINGERPRINT_SQL)
    return {"edge_count": int(r["edge_count"]), "edge_hash": int(r["edge_hash"])}


async def last_fingerprint(conn) -> dict | None:
    """Return the most recent persisted build fingerprint, or None if the indices were never built."""
    row = await conn.fetchrow(
        "SELECT payload FROM nexus_event WHERE kind = $1 ORDER BY created_at DESC LIMIT 1", _BUILD_EVENT_KIND
    )
    if not row or row["payload"] is None:
        return None
    p = row["payload"]
    return json.loads(p) if isinstance(p, str) else dict(p)


def pagerank(g: PPRGraph, *, restart: float = 0.15, max_iter: int = 200, tol: float = 1e-9) -> np.ndarray:
    """Compute global PageRank by power iteration.

    r = restart * u + (1 - restart) * (T^T r + dangling_mass * u), with u the uniform teleport vector.
    """
    if g.n == 0:
        return np.zeros(0)
    u = np.full(g.n, 1.0 / g.n)
    r = u.copy()
    for _ in range(max_iter):
        dmass = float(r[g.dangling].sum())  # dangling mass teleports uniformly
        rn = restart * u + (1.0 - restart) * (g.Tt @ r + dmass * u)
        rn /= rn.sum()
        if np.abs(rn - r).sum() < tol:
            r = rn
            break
        r = rn
    return r


async def load_current_law_edges() -> list[tuple[str, str, float]]:
    """Read the current-law, self-loop-free edge set once; it feeds both graph views."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_EDGE_SQL)
    return [(r["src"], r["dst"], float(r["w"])) for r in rows]


def directed_graph(edges: list[tuple[str, str, float]]) -> PPRGraph:
    """Build the transposed row-stochastic transition matrix for PageRank, as ppr does."""
    ids = sorted({s for s, _, _ in edges} | {d for _, d, _ in edges})
    idx = {u: i for i, u in enumerate(ids)}
    n, m = len(ids), len(edges)
    R, Cc, W = np.empty(m, np.int32), np.empty(m, np.int32), np.empty(m, np.float64)
    for k, (s, d, w) in enumerate(edges):
        R[k], Cc[k], W[k] = idx[s], idx[d], w
    A = sp.csr_matrix((W, (R, Cc)), shape=(n, n))
    outdeg = np.asarray(A.sum(axis=1)).ravel()
    dangling = outdeg == 0
    inv = np.zeros(n)
    inv[~dangling] = 1.0 / outdeg[~dangling]
    Tt = (sp.diags(inv) @ A).T.tocsr()
    return PPRGraph(ids, idx, Tt, dangling, n)


def undirected_graph(edges: list[tuple[str, str, float]]) -> nx.Graph:
    """Undirected weighted graph (reciprocal weights summed) for Louvain regime clustering."""
    g = nx.Graph()
    for s, d, w in edges:
        if g.has_edge(s, d):
            g[s][d]["weight"] += w
        else:
            g.add_edge(s, d, weight=w)
    return g


def communities(g: nx.Graph, *, seed: int = _SEED) -> dict[str, int]:
    """Cluster regimes with Louvain and return ``{provision_id: community_id}`` (0 = largest)."""
    if g.number_of_nodes() == 0:
        return {}
    parts = louvain_communities(g, weight="weight", seed=seed)
    parts = sorted(parts, key=len, reverse=True)  # stable, size-ranked ids
    return {node: cid for cid, comm in enumerate(parts) for node in comm}


async def build_intelligence(*, apply: bool = False, top: int = 8, force: bool = False) -> dict:
    """Compute centrality and communities, and write them to `provision` when `apply` is set.

    Returns a summary for reporting: mode, node and community counts, the edge fingerprint, the top
    hubs and the anchors of the largest regimes.
    """
    # Freshness short-circuit: if the live edge-set hash equals the last persisted fingerprint, the
    # result cannot have changed, so the expensive PageRank and Louvain run and all writes are skipped.
    # Comparing the hash, not just the count, catches count-preserving edits. The cost is one aggregate
    # scan plus one small read. `force=True` bypasses the check; a dry run always recomputes.
    pool = await get_pool()
    if apply and not force:
        async with pool.acquire() as conn:
            await conn.execute(_EVENT_DDL)
            live = await current_fingerprint(conn)
            fp = await last_fingerprint(conn)
        if fp is not None and fp.get("edge_hash") == live["edge_hash"] and fp.get("edge_count") == live["edge_count"]:
            return {
                "mode": "SKIPPED (fresh — fingerprint matches live edge-set hash)",
                "n": fp.get("node_count"),
                "n_comm": fp.get("n_communities"),
                "edge_count": live["edge_count"],
                "edge_hash": live["edge_hash"],
                "fingerprint": fp,
                "top": [],
                "anchors": [],
            }

    edges = await load_current_law_edges()
    edge_count = len(edges)
    edge_hash: int | None = None  # set inside the apply transaction (the committed content hash)
    g = directed_graph(edges)
    r = pagerank(g)
    ug = undirected_graph(edges)
    comm = communities(ug)
    order = np.argsort(r)[::-1]
    n_comm = (max(comm.values()) + 1) if comm else 0
    top_hub = g.ids[int(order[0])] if g.n else None

    async with pool.acquire() as conn:
        if apply:
            await conn.execute(_DDL)
            await conn.execute(_EVENT_DDL)
            # The desired state: every node in the graph gets both centrality and community (it has
            # at least one in-force, traversable, non-self edge). centrality is stored as Postgres
            # `real` (single precision), so the float64 score is round-tripped through float32 to
            # equal what a read-back returns. Otherwise an unchanged re-apply would see every row as
            # changed and the write would no longer be idempotent.
            desired = {g.ids[i]: (float(np.float32(r[i])), int(comm[g.ids[i]])) for i in range(g.n)}
            node_set = set(desired)

            # Read the stored values so only the delta is written (an unchanged re-apply writes no
            # rows) and the NULL reset touches only rows that actually leave the scored set.
            stored = {
                str(row["id"]): (row["centrality"], row["community"])
                for row in await conn.fetch(
                    "SELECT id, centrality, community FROM provision "
                    "WHERE centrality IS NOT NULL OR community IS NOT NULL"
                )
            }

            # Changed rows are scored nodes whose stored pair differs from the desired one. The UPDATE
            # re-checks the same condition with IS DISTINCT FROM.
            changed = [(pid, cen, cm) for pid, (cen, cm) in desired.items() if stored.get(pid) != (cen, cm)]
            # Reset only rows leaving the scored set (currently non-NULL, no longer a node). Newly
            # isolated provisions become NULL without blanking the whole table.
            leaving = [pid for pid in stored if pid not in node_set]

            # One transaction, so the explorer never sees a half-reset or half-stale layer.
            async with conn.transaction():
                if leaving:
                    await conn.execute(
                        "UPDATE provision SET centrality = NULL, community = NULL WHERE id = ANY($1::uuid[])", leaving
                    )
                if changed:
                    # Bulk path: stage the changed rows in a temp table (dropped on commit), then run
                    # a single UPDATE ... FROM, avoiding per-row round trips.
                    await conn.execute(
                        "CREATE TEMP TABLE _intel_delta (id uuid PRIMARY KEY, "
                        "centrality real, community int) ON COMMIT DROP"
                    )
                    await conn.copy_records_to_table(
                        "_intel_delta",
                        records=[(pid, cen, cm) for pid, cen, cm in changed],
                        columns=("id", "centrality", "community"),
                    )
                    await conn.execute(
                        "UPDATE provision p SET centrality = d.centrality, community = d.community "
                        "FROM _intel_delta d WHERE p.id = d.id "
                        "AND (p.centrality IS DISTINCT FROM d.centrality "
                        "     OR p.community IS DISTINCT FROM d.community)"
                    )
                # Persist the build fingerprint in the same transaction as the data, so both commit
                # or roll back together and a reader never sees a "fresh" fingerprint on a half-written
                # layer. edge_hash is read inside the transaction, so it reflects exactly the committed
                # edge set; the other fields identify the build for humans.
                live_fp = await current_fingerprint(conn)
                edge_hash = live_fp["edge_hash"]
                fingerprint = {
                    "edge_count": live_fp["edge_count"],
                    "edge_hash": edge_hash,
                    "node_count": g.n,
                    "n_communities": n_comm,
                    "top_hub": top_hub,
                    "seed": _SEED,
                    "built_at": datetime.now(UTC).isoformat(),
                }
                await conn.execute(
                    "INSERT INTO nexus_event (kind, payload) VALUES ($1, $2::jsonb)",
                    _BUILD_EVENT_KIND,
                    json.dumps(fingerprint, ensure_ascii=False),
                )
        top_ids = [g.ids[int(i)] for i in order[:top]]
        labels = {
            str(x["id"]): x
            for x in await conn.fetch(
                """SELECT p.id::text id, p.label, sd.external_id ext, left(sd.title, 30) title
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE p.id = ANY($1::uuid[])""",
                top_ids,
            )
        }

        # Regime anchors: for the largest communities, the most central member serves as the label.
        sizes: dict[int, int] = {}
        for cid in comm.values():
            sizes[cid] = sizes.get(cid, 0) + 1
        anchors = []
        for cid, _ in sorted(sizes.items(), key=lambda kv: -kv[1])[:5]:
            members = [(g.idx[pid], pid) for pid in comm if comm[pid] == cid and pid in g.idx]
            if not members:
                continue
            best = max(members, key=lambda mp: r[mp[0]])[1]
            row = await conn.fetchrow(
                """SELECT p.label, sd.external_id ext, left(sd.title, 34) title
                     FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                    WHERE p.id = $1::uuid""",
                best,
            )
            anchors.append((cid, sizes[cid], dict(row) if row else {}))

    return {
        "mode": "APPLIED" if apply else "DRY-RUN (no write)",
        "n": g.n,
        "n_comm": n_comm,
        "edge_count": edge_count,
        "edge_hash": edge_hash,
        "top_hub": top_hub,
        "top": [
            (
                labels.get(i, {}).get("ext", "?"),
                labels.get(i, {}).get("label", "?"),
                labels.get(i, {}).get("title", ""),
                float(r[int(order[k])]),
            )
            for k, i in enumerate(top_ids)
        ],
        "anchors": anchors,
    }


async def reindex_intelligence(*, force: bool = False) -> dict:
    """Refresh centrality, communities and the fingerprint after ingestion.

    A thin wrapper over ``build_intelligence(apply=True)``, so every ingest path that changes the
    graph has one obvious call to make, and all of them share the freshness short-circuit (a no-op
    when the edge set is unchanged). Called from refresh._reindex (the e-Sbírka amendment path) and
    the statute finalisation; the court, ČNB and EUR-Lex CLIs call it once their rows are
    materialised.

    Serialised by a Postgres advisory lock (_REINDEX_LOCK_KEY): if another reindex is running, this
    one waits, and the short-circuit then makes it a near-instant no-op. This prevents concurrent
    reindexes from leaving the partition and fingerprint inconsistent. The lock is held on a
    dedicated connection for the whole read, compute and write, and released in `finally`, so a crash
    mid-build cannot leave it held.
    """
    pool = await get_pool()
    async with pool.acquire() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock($1)", _REINDEX_LOCK_KEY)
        try:
            res = await build_intelligence(apply=True, force=force)
        finally:
            await lock_conn.execute("SELECT pg_advisory_unlock($1)", _REINDEX_LOCK_KEY)
    # The edge set may have just changed, so clear the in-memory PPR cache here, where every reindex
    # path passes. The per-view fingerprint is authoritative; this is an extra safeguard for any
    # serving path in this process.
    clear_ppr_cache()
    return res


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DOWN)
        print("⏪ reverted: provision.centrality + provision.community dropped.")
        await close_pool()
        return
    res = await build_intelligence(apply=("--apply" in sys.argv), force=("--force" in sys.argv))
    n = res["n"] or 0
    print(f"=== N6 graph intelligence — {res['mode']} · {n:,} nodes · {res['n_comm']} regimes ===")
    if res["mode"].startswith("SKIPPED"):
        print(f"  fingerprint fresh (live edge_count={res.get('edge_count')}) — no recompute, no write.")
        await close_pool()
        return
    print("  top structural hubs (centrality — should be definition/scope articles):")
    for ext, label, title, score in res["top"]:
        print(f"    {str(ext):11s} {str(label):11s} {score:.5f}  {title}")
    print("  largest regimes (community — anchor = highest-centrality member):")
    for cid, size, a in res["anchors"]:
        print(
            f"    #{cid:<3d} {size:5d} provisions   anchor {str(a.get('ext', '?')):11s} "
            f"{str(a.get('label', '?')):11s} {a.get('title', '')}"
        )
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --apply to write)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
