"""N6 — graph-intelligence centrality (pure, no DB): global PageRank on a synthetic graph."""

from __future__ import annotations

import networkx as nx
import numpy as np
import scipy.sparse as sp

from pythia.graph.intelligence import communities, pagerank
from pythia.graph.ppr import PPRGraph


def _graph(adj: np.ndarray) -> PPRGraph:
    """Build a PPRGraph from a dense weighted adjacency (rows = src), mirroring load_ppr_graph."""
    A = sp.csr_matrix(adj.astype(float))
    n = adj.shape[0]
    outdeg = np.asarray(A.sum(axis=1)).ravel()
    dangling = outdeg == 0
    inv = np.zeros(n)
    inv[~dangling] = 1.0 / outdeg[~dangling]
    Tt = (sp.diags(inv) @ A).T.tocsr()
    ids = [f"n{i}" for i in range(n)]
    return PPRGraph(ids, {u: i for i, u in enumerate(ids)}, Tt, dangling, n)


def test_pagerank_is_a_probability_distribution():
    g = _graph(np.array([[0, 1, 1], [1, 0, 1], [1, 1, 0]]))
    r = pagerank(g)
    assert abs(r.sum() - 1.0) < 1e-6 and (r >= 0).all()


def test_hub_outranks_its_satellites():
    # 1,2,3 all point at 0 (the hub); 0 points back only to 1 — node 0 must win on centrality.
    adj = np.array(
        [
            [0, 1, 0, 0],
            [1, 0, 0, 0],
            [1, 0, 0, 0],
            [1, 0, 0, 0],
        ]
    )
    r = pagerank(_graph(adj))
    assert int(np.argmax(r)) == 0
    assert r[0] > r[1] and r[0] > r[2] and r[0] > r[3]


def test_empty_graph_is_safe():
    g = PPRGraph([], {}, sp.csr_matrix((0, 0)), np.zeros(0, bool), 0)
    assert pagerank(g).shape == (0,)


def test_dangling_node_mass_is_conserved():
    # node 1 is a sink (no out-edges) — mass must still normalise to 1, not leak away.
    adj = np.array([[0, 1], [0, 0]])
    r = pagerank(_graph(adj))
    assert abs(r.sum() - 1.0) < 1e-6


def test_communities_separate_two_cliques():
    # two tight triangles joined by a single thin bridge — Louvain must split them.
    g = nx.Graph()
    for a, b in [("a", "b"), ("b", "c"), ("a", "c")]:
        g.add_edge(a, b, weight=5.0)
    for a, b in [("x", "y"), ("y", "z"), ("x", "z")]:
        g.add_edge(a, b, weight=5.0)
    g.add_edge("c", "x", weight=0.1)  # the weak bridge
    comm = communities(g)
    assert comm["a"] == comm["b"] == comm["c"]
    assert comm["x"] == comm["y"] == comm["z"]
    assert comm["a"] != comm["x"]  # two distinct regimes


def test_communities_are_size_ranked():
    # a 4-clique + a 3-clique → the larger cluster must be community 0.
    g = nx.Graph()
    big = ["a", "b", "c", "d"]
    small = ["w", "x", "y"]
    for i, u in enumerate(big):
        for v in big[i + 1 :]:
            g.add_edge(u, v, weight=5.0)
    for i, u in enumerate(small):
        for v in small[i + 1 :]:
            g.add_edge(u, v, weight=5.0)
    g.add_edge("d", "w", weight=0.1)
    comm = communities(g)
    assert comm["a"] == 0  # largest cluster relabelled to 0


def test_empty_community_graph_is_safe():
    assert communities(nx.Graph()) == {}
