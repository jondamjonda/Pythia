"""PPR default-view graph cache — the RIGIDITY guarantees (task 2).

These tests pin the cache's correctness contract WITHOUT a DB: the pool, the N6 fingerprint, and the
expensive edge-loader are all patched, so we exercise the cache LOGIC in isolation —
  • a cache hit on an unchanged fingerprint never rebuilds,
  • ANY fingerprint move (even count-preserving — a rewire/reweight) invalidates,
  • clear_ppr_cache() forces a rebuild,
  • concurrent callers trigger EXACTLY ONE rebuild (lock + double-check, no thundering herd),
  • ppr_retrieve routes ONLY the default view through the cache; an explicit as_of or the case-law
    lane (or a caller-supplied graph) always builds/uses fresh.
"""

from __future__ import annotations

import asyncio
from datetime import date

import numpy as np
import scipy.sparse as sp

import pythia.graph.ppr as ppr
from pythia.graph.ppr import PPRGraph


def _sentinel_graph(tag: str = "x", n: int = 3) -> PPRGraph:
    """A tiny real PPRGraph (complete digraph) so ppr_scores can run on it in the routing test."""
    adj = np.ones((n, n)) - np.eye(n)
    A = sp.csr_matrix(adj)
    outdeg = np.asarray(A.sum(axis=1)).ravel()
    dangling = outdeg == 0
    inv = np.zeros(n)
    inv[~dangling] = 1.0 / outdeg[~dangling]
    Tt = (sp.diags(inv) @ A).T.tocsr()
    ids = [f"{tag}{i}" for i in range(n)]
    return PPRGraph(ids, {u: i for i, u in enumerate(ids)}, Tt, dangling, n)


def _patch_env(monkeypatch, fp_holder, builds, *, load_delay: float = 0.0):
    """Patch the DB pool + the view fingerprint + the edge-loader, and give the cache a FRESH lock bound to
    the current test's event loop (asyncio_mode=auto runs each test in its own loop). The fake connection
    supports .transaction(...) so the cache's atomic single-snapshot rebuild path runs."""

    class _Txn:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    class _Conn:
        def transaction(self, **kw):
            return _Txn()

    class _Acq:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acq()

    async def _get_pool():
        return _Pool()

    async def _view_fingerprint(conn, as_of, include_case_law):
        return dict(fp_holder["fp"])

    async def _load(as_of=None, *, include_case_law=False, conn=None):
        builds.append(1)
        if load_delay:
            await asyncio.sleep(load_delay)
        return _sentinel_graph(f"build{len(builds)}")

    monkeypatch.setattr(ppr, "get_pool", _get_pool)
    monkeypatch.setattr(ppr, "_view_fingerprint", _view_fingerprint)
    monkeypatch.setattr(ppr, "load_ppr_graph", _load)
    monkeypatch.setattr(ppr, "_CACHE_LOCK", asyncio.Lock())
    ppr.clear_ppr_cache()


async def test_cache_hit_same_fingerprint_no_rebuild(monkeypatch):
    builds: list[int] = []
    _patch_env(monkeypatch, {"fp": {"edge_count": 10, "edge_hash": 111}}, builds)
    g1 = await ppr.get_default_ppr_graph()
    g2 = await ppr.get_default_ppr_graph()
    assert g1 is g2  # the SAME object is reused
    assert len(builds) == 1  # built exactly once
    ppr.clear_ppr_cache()


async def test_count_preserving_mutation_invalidates(monkeypatch):
    # edge_count stays 10 but the content hash moves (a rewire / reweight / net-zero delete+insert):
    # the cache MUST rebuild — this is the exact blind spot a count-only key would miss.
    fp = {"fp": {"edge_count": 10, "edge_hash": 111}}
    builds: list[int] = []
    _patch_env(monkeypatch, fp, builds)
    g1 = await ppr.get_default_ppr_graph()
    fp["fp"] = {"edge_count": 10, "edge_hash": 222}
    g2 = await ppr.get_default_ppr_graph()
    assert g1 is not g2
    assert len(builds) == 2
    ppr.clear_ppr_cache()


async def test_clear_forces_rebuild(monkeypatch):
    builds: list[int] = []
    _patch_env(monkeypatch, {"fp": {"edge_count": 10, "edge_hash": 111}}, builds)
    await ppr.get_default_ppr_graph()
    ppr.clear_ppr_cache()
    await ppr.get_default_ppr_graph()  # same fingerprint, but the cache was cleared → rebuild
    assert len(builds) == 2
    ppr.clear_ppr_cache()


async def test_concurrent_calls_rebuild_once(monkeypatch):
    builds: list[int] = []
    _patch_env(monkeypatch, {"fp": {"edge_count": 10, "edge_hash": 111}}, builds, load_delay=0.02)
    results = await asyncio.gather(*[ppr.get_default_ppr_graph() for _ in range(8)])
    assert len(builds) == 1  # lock + double-check → exactly one rebuild
    assert all(r is results[0] for r in results)  # all 8 callers share the one graph
    ppr.clear_ppr_cache()


async def test_retrieve_routes_only_default_view_through_cache(monkeypatch):
    used: list = []
    g_default = _sentinel_graph("def")
    g_fresh = _sentinel_graph("fresh")

    async def _get_default():
        used.append("cache")
        return g_default

    async def _load(as_of=None, *, include_case_law=False, conn=None):
        used.append(("load", as_of, include_case_law))
        return g_fresh

    monkeypatch.setattr(ppr, "get_default_ppr_graph", _get_default)
    monkeypatch.setattr(ppr, "load_ppr_graph", _load)

    await ppr.ppr_retrieve(g_default.ids[:1], k=2)  # default → cache
    await ppr.ppr_retrieve(g_fresh.ids[:1], k=2, as_of=date(2020, 1, 1))  # explicit as_of → fresh
    await ppr.ppr_retrieve(g_fresh.ids[:1], k=2, include_case_law=True)  # case-law lane → fresh
    await ppr.ppr_retrieve(g_fresh.ids[:1], k=2, graph=g_fresh)  # supplied graph → neither

    assert used[0] == "cache"
    assert used[1][0] == "load" and used[1][1] == date(2020, 1, 1)
    assert used[2][0] == "load" and used[2][2] is True
    assert len(used) == 3  # the supplied-graph call hit neither the cache nor a load
    ppr.clear_ppr_cache()
