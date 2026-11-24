"""Tensor-loader checks (skip the sentence-model download for speed)."""

from __future__ import annotations

import pytest

from pythia.db import close_pool
from pythia.graph.tensors import load_legal_graph


@pytest.mark.asyncio
async def test_graph_tensors_consistent():
    g = await load_legal_graph(with_relation_emb=False)
    e = g.edge_src.numel()
    assert e > 10_000, "graph should have many edges"
    assert g.edge_dst.numel() == e and g.edge_rel.numel() == e and g.edge_weight.numel() == e
    assert int(g.edge_src.max()) < g.n_nodes and int(g.edge_dst.max()) < g.n_nodes
    assert int(g.edge_rel.max()) < g.n_relations
    assert g.n_relations == len(g.rel_labels) == len(g.rel_ids)
    assert float(g.edge_weight.min()) > 0.0
    await close_pool()
