"""Personalized PageRank (PPR) retriever over the legal graph.

Training-free and LLM-free. The whole graph fits in memory as a sparse matrix, so PPR is a strong
multi-hop ranker that often rivals a GNN without any training; it is the bar the GNN must clear.
The random walk follows out-edges in proportion to edge weight (w0 * c) and restarts at the seed
set with probability `restart`. Only law in force is included, using the same temporal mask as the
CTE walk.

Unlike the CTE walk and the GNN, PPR yields scores but no discrete typed paths; explanatory paths
are one of the GNN's advantages.

Run:  .venv/bin/python -m pythia.graph.ppr [seed_uuid]
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import date

import numpy as np
import scipy.sparse as sp

from pythia.db import close_pool, get_pool
from pythia.graph.cte_expand import NeighborHit, four_corners
from pythia.graph.relations import NON_TRAVERSABLE_REL_IDS
from pythia.graph.seed import current_only_clause  # shared current-only predicate

# The PPR edge view joins two provision endpoints (ps = src, pd = dst), so the fail-open current-only
# predicate is applied to both; seed.current_only_clause renders it once per alias.
_PPR_CURRENT_ONLY_ALIASES = ("ps", "pd")

# annuls/annulled_by (27/28) record status lineage, not navigation, and are excluded from the walk.
_PPR_NON_TRAVERSABLE = "(" + ",".join(str(i) for i in NON_TRAVERSABLE_REL_IDS) + ")"


# The single definition of the PPR edge set, for every view. Parameters: $1 as_of (NULL means
# CURRENT_DATE), $2 include_case_law. load_ppr_graph builds the transition matrix from these rows and
# the cache fingerprint hashes the same rows. Sharing one predicate matters: if the loader and the
# fingerprint ever filtered on different columns (say the loader dropped CASE_LAW endpoints and the
# fingerprint did not), a change to that column would alter the cached graph without changing its
# fingerprint, and a stale graph would be served.
def _ppr_edge_select(current_only: str = "") -> str:
    """Return the SQL that selects the PPR edge set, with the optional current-only clause appended.

    `current_only` comes from ``seed.current_only_clause(..., aliases=('ps', 'pd'))``. It is empty
    unless temporal_current_only_enabled is on, in which case it adds the fail-open point-in-time
    predicate for both endpoints. The fingerprint SQL wraps this exact select, so toggling the flag
    changes the edge set, moves the fingerprint and makes the cached graph rebuild.
    """
    return f"""
SELECT e.src::text AS src, e.dst::text AS dst, e.weight::float8 AS w
  FROM pythia_edge e
  JOIN provision ps ON ps.id = e.src
  JOIN provision pd ON pd.id = e.dst
 WHERE e.dst IS NOT NULL
   AND e.src <> e.dst
   AND e.rel_id NOT IN {_PPR_NON_TRAVERSABLE}
   AND ($2::boolean OR (ps.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                        AND pd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'))
   AND (ps.valid_to IS NULL OR ps.valid_to > COALESCE($1::date, CURRENT_DATE))
   AND (pd.valid_to IS NULL OR pd.valid_to > COALESCE($1::date, CURRENT_DATE))
   AND (ps.valid_from IS NULL OR ps.valid_from <= COALESCE($1::date, CURRENT_DATE))  -- both endpoints
   AND (pd.valid_from IS NULL OR pd.valid_from <= COALESCE($1::date, CURRENT_DATE))  -- already in force at as_of{current_only}
"""


# Freshness fingerprint of one PPR view, computed server-side without loading rows (one aggregate scan;
# bit_xor is order-independent). It hashes the same src|dst|w rows the edge select yields for the given
# $1 as_of and $2 include_case_law, so it changes whenever that view changes: an edge is added, removed
# or reweighted, a valid_to date passes (including the daily CURRENT_DATE rollover), or a
# legal_hierarchy change adds or drops a case-law edge from the statute-only set.
def _ppr_fingerprint_sql(current_only: str = "") -> str:
    """Return the fingerprint SQL for the PPR view, wrapping ``_ppr_edge_select(current_only)``.

    Because it wraps the same select, the fingerprint also tracks the current-only flag. The count
    alone would not be enough: a count-preserving rewire keeps the count but changes the hash.
    """
    return f"""
SELECT count(*) AS edge_count,
       COALESCE(bit_xor(hashtext(src || '|' || dst || '|' || w::text)), 0) AS edge_hash
  FROM ({_ppr_edge_select(current_only)}) _e
"""


@dataclass
class PPRGraph:
    """An in-memory sparse transition matrix over provisions, ready for power iteration."""

    ids: list[str]  # node index -> provision uuid
    idx: dict[str, int]  # provision uuid -> node index
    Tt: sp.csr_matrix  # transpose of the row-stochastic transition (for Tt @ r)
    dangling: np.ndarray  # bool mask of out-degree-0 nodes
    n: int


async def _view_fingerprint(conn, as_of: date | None, include_case_law: bool) -> dict:
    """Return ``{edge_count, edge_hash}`` for the PPR view defined by `as_of` and `include_case_law`.

    This is the cache's freshness signal, hashed over the same predicate the graph is loaded from.
    """
    current_only = await current_only_clause(conn, aliases=_PPR_CURRENT_ONLY_ALIASES)  # tracks the flag
    r = await conn.fetchrow(_ppr_fingerprint_sql(current_only), as_of, include_case_law)
    return {"edge_count": int(r["edge_count"]), "edge_hash": int(r["edge_hash"])}


async def load_ppr_graph(as_of: date | None = None, *, include_case_law: bool = False, conn=None) -> PPRGraph:
    """Load `pythia_edge` into an in-memory sparse transition matrix (law in force only).

    By default (`include_case_law=False`) edges with a CASE_LAW endpoint are dropped, so the random
    walk never crosses into rulings and a statute retrieval stays statute-only. annuls/annulled_by
    (27/28) are always excluded. `include_case_law=True` is for the explicit opt-in path.

    `conn` lets the caller share one snapshot between the edge load and a fingerprint read (used by
    the cache's atomic rebuild); when None, a pooled connection is acquired.
    """
    if conn is not None:
        current_only = await current_only_clause(conn, aliases=_PPR_CURRENT_ONLY_ALIASES)
        rows = await conn.fetch(_ppr_edge_select(current_only), as_of, include_case_law)
    else:
        pool = await get_pool()
        async with pool.acquire() as _conn:
            current_only = await current_only_clause(_conn, aliases=_PPR_CURRENT_ONLY_ALIASES)
            rows = await _conn.fetch(_ppr_edge_select(current_only), as_of, include_case_law)
    ids = sorted({r["src"] for r in rows} | {r["dst"] for r in rows})
    idx = {u: i for i, u in enumerate(ids)}
    n, m = len(ids), len(rows)
    R = np.empty(m, np.int32)
    Cc = np.empty(m, np.int32)
    W = np.empty(m, np.float64)
    for k, r in enumerate(rows):
        R[k], Cc[k], W[k] = idx[r["src"]], idx[r["dst"]], r["w"]
    A = sp.csr_matrix((W, (R, Cc)), shape=(n, n))  # weighted adjacency
    outdeg = np.asarray(A.sum(axis=1)).ravel()
    dangling = outdeg == 0
    inv = np.zeros(n)
    inv[~dangling] = 1.0 / outdeg[~dangling]
    Tt = (sp.diags(inv) @ A).T.tocsr()  # row-stochastic transition, transposed
    return PPRGraph(ids, idx, Tt, dangling, n)


# ── Default-view graph cache (invalidated by fingerprint) ────────────────────────────────────────────
# Rebuilding the sparse transition matrix on every ppr_retrieve dominated method='ppr' latency. Only the
# hot default view is cached (as_of=None, i.e. current law, and statute-only), and it is invalidated by
# a fingerprint of that same view (_view_fingerprint(None, False)). Every other view (an explicit as_of,
# the case-law lane, or a caller-supplied graph) is always built fresh.
_DEFAULT_CACHE: dict | None = None  # {"graph": PPRGraph, "fp": {edge_count, edge_hash}} or None
_CACHE_LOCK = asyncio.Lock()  # serialises rebuilds (one rebuild per fingerprint change)


def clear_ppr_cache() -> None:
    """Drop the cached default PPR graph.

    The view fingerprint is the authoritative invalidation in production. This function exists for
    test isolation and as an extra safeguard wherever the edge set is rebuilt; reindex_intelligence,
    which every reindex path goes through, calls it.
    """
    global _DEFAULT_CACHE
    _DEFAULT_CACHE = None


async def get_default_ppr_graph() -> PPRGraph:
    """Return the cached default-view PPR graph (current law, statute-only).

    The graph is rebuilt only when that view's edge set has changed, and a stale graph is never
    served:

    - Invalidation uses `_view_fingerprint(None, False)`: a count plus
      bit_xor(hashtext(src|dst|w)) over exactly the predicate the graph is loaded from. It changes on
      any change to the view: an edge added, removed or reweighted, a valid_to date passing
      (including the daily CURRENT_DATE rollover), or a legal_hierarchy change that adds or drops a
      case-law edge. Because loader and fingerprint share one predicate, the fingerprint cannot miss
      a column the view filters on.
    - The rebuild reads the fingerprint and loads the edges on one connection inside a single
      REPEATABLE READ transaction, so both come from the same snapshot and a cached graph always
      matches its own fingerprint. A later match on the hot path therefore guarantees the same edge
      set, which rules out an A-to-B-to-A race that two separate snapshots would allow.

    The hot-path check is one cheap server-side aggregate (no rows loaded); the expensive edge load
    and matrix build run only after a real change.
    """
    global _DEFAULT_CACHE
    pool = await get_pool()
    async with pool.acquire() as conn:  # hot path: one cheap aggregate, no rebuild
        fp = await _view_fingerprint(conn, None, False)
    cached = _DEFAULT_CACHE
    if cached is not None and cached["fp"] == fp:
        return cached["graph"]
    async with _CACHE_LOCK:  # serialise, so concurrent callers do not all rebuild
        # Atomic rebuild: fingerprint and edge load in one repeatable-read snapshot, so they are
        # consistent (no check-then-load race). Re-check the cache first: another caller may have
        # rebuilt it while this one waited for the lock.
        async with pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                fp = await _view_fingerprint(conn, None, False)
                cached = _DEFAULT_CACHE
                if cached is not None and cached["fp"] == fp:
                    return cached["graph"]
                g = await load_ppr_graph(None, include_case_law=False, conn=conn)
        _DEFAULT_CACHE = {"graph": g, "fp": fp}
        return g


def ppr_scores(
    g: PPRGraph, seeds: list[tuple[str, float]], *, restart: float = 0.15, max_iter: int = 100, tol: float = 1e-6
) -> np.ndarray:
    """Compute personalized PageRank scores by power iteration.

    Iterates r = restart * s + (1 - restart) * (T^T r + dangling_mass * s) until convergence, where s
    is the normalised seed vector.
    """
    s = np.zeros(g.n)
    for sid, w in seeds:
        i = g.idx.get(sid)
        if i is not None:
            s[i] += max(float(w), 1e-9)
    if s.sum() == 0:
        return s
    s /= s.sum()
    r = s.copy()
    for _ in range(max_iter):
        dmass = float(r[g.dangling].sum())  # dangling mass teleports back to seeds
        rn = restart * s + (1.0 - restart) * (g.Tt @ r + dmass * s)
        rn /= rn.sum()
        if np.abs(rn - r).sum() < tol:
            r = rn
            break
        r = rn
    return r


async def ppr_retrieve(
    seed_ids: list[str],
    *,
    k: int = 20,
    as_of: date | None = None,
    restart: float = 0.15,
    graph: PPRGraph | None = None,
    include_case_law: bool = False,
) -> list[NeighborHit]:
    """Rank provisions by personalized PageRank from `seed_ids` and return the top `k` non-seeds.

    PPR hits carry a score but no relation path (hops=0, relation_path=[]).
    """
    # The default view (current law, statute-only) uses the cached graph (about 20x faster on a hit).
    # A caller-supplied graph, an explicit as_of or the case-law lane builds or uses its own graph.
    if graph is not None:
        g = graph
    elif as_of is None and not include_case_law:
        g = await get_default_ppr_graph()
    else:
        g = await load_ppr_graph(as_of, include_case_law=include_case_law)
    r = ppr_scores(g, [(s, 1.0) for s in seed_ids], restart=restart)
    seedset = set(seed_ids)
    out: list[NeighborHit] = []
    for i in np.argsort(-r):
        pid = g.ids[i]
        if r[i] <= 0 or pid in seedset:  # exclude the seeds themselves
            continue
        out.append(NeighborHit(pid, hops=0, score=float(r[i]), relation_path=[]))  # PPR has no paths
        if len(out) >= k:
            break
    return out


async def _demo(seed: str | None) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if seed is None:
            seed = str(
                await conn.fetchval(
                    """SELECT src FROM pythia_edge WHERE edge_type IN ('references','internal_reference')
                   GROUP BY src ORDER BY count(*) DESC LIMIT 1"""
                )
            )
        meta = await conn.fetchrow(
            """SELECT p.label, sd.title FROM provision p
                 JOIN source_document sd ON sd.id = p.source_document_id WHERE p.id = $1::uuid""",
            seed,
        )
    t0 = time.perf_counter()
    g = await load_ppr_graph()
    t_load = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    ppr_hits = await ppr_retrieve([seed], k=20, graph=g)
    t_ppr = (time.perf_counter() - t0) * 1000
    cte_hits = await four_corners([seed], max_hops=3, k=20)
    pset = {h.provision_id for h in ppr_hits}
    cset = {h.provision_id for h in cte_hits}
    print(f"SEED {meta['label']} · {(meta['title'] or '')[:50]}")
    print(f"graph: {g.n} nodes loaded in {t_load:.0f} ms · PPR solve {t_ppr:.0f} ms")
    print(f"PPR top-20 ∩ CTE top-20 overlap: {len(pset & cset)}/20  (agreement sanity-check)")
    ids = [h.provision_id for h in ppr_hits[:8]]
    async with pool.acquire() as conn:
        labels = {
            str(r["id"]): dict(r)
            for r in await conn.fetch(
                """SELECT p.id, p.label, sd.title FROM provision p
                 JOIN source_document sd ON sd.id = p.source_document_id WHERE p.id = ANY($1::uuid[])""",
                ids,
            )
        }
    print("top PPR-ranked provisions:")
    for h in ppr_hits[:8]:
        m = labels.get(h.provision_id, {})
        print(f"  s={h.score:.5f}  {(m.get('label') or '?'):<18} {(m.get('title') or '')[:42]}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_demo(sys.argv[1] if len(sys.argv) > 1 else None))
