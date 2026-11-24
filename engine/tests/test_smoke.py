"""Smoke tests for the P0–P1.5 engine (require the live Polarius Postgres on :5432)."""

from __future__ import annotations

import pytest

from pythia.db import close_pool
from pythia.graph import relations
from pythia.graph.cte_expand import four_corners
from pythia.graph.ppr import load_ppr_graph, ppr_retrieve
from pythia.graph.seed import sparse_seed


def test_relations_registry_consistency():
    labels = {r.label for r in relations.R0}
    assert len({r.rel_id for r in relations.R0}) == len(relations.R0), "duplicate rel_id"
    for r in relations.R0:
        assert r.inverse_of in labels, f"{r.label}: inverse {r.inverse_of!r} not registered"
        assert relations.BY_ID[r.rel_id] is r
        assert relations.BY_LABEL[r.label] is r
        assert r.description, f"{r.label}: empty description (needed for embedding)"


@pytest.mark.asyncio
async def test_seed_then_four_corners_and_ppr():
    seeds = await sparse_seed("investiční fond povinnost notifikace", k=5)
    assert seeds, "sparse seed returned nothing"
    seed_ids = [s.provision_id for s in seeds]

    cte = await four_corners(seed_ids, max_hops=2, k=10)
    assert cte, "CTE four-corners returned nothing"
    assert all(h.relation_path for h in cte), "CTE hits must carry a typed relation_path"

    graph = await load_ppr_graph()
    assert graph.n > 1000, "PPR graph suspiciously small"
    ppr = await ppr_retrieve(seed_ids, k=10, graph=graph)
    assert ppr, "PPR returned nothing"
    await close_pool()
