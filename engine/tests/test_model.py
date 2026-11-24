"""GNN model checks on a tiny toy graph (no DB, CPU)."""

from __future__ import annotations

import torch

from pythia.gnn.model import LegalGFM, n_params

DIM = 768  # toy embedding dim; passed explicitly so the test is independent of the model's default
# (the real callers pass g.relation_emb.shape[1] — 1024 for bge-m3 in production)


class _ToyGraph:
    n_nodes = 5
    relation_emb = torch.randn(2, DIM)
    edge_src = torch.tensor([0, 1, 2, 3])
    edge_dst = torch.tensor([1, 2, 3, 4])
    edge_rel = torch.tensor([0, 0, 1, 1])
    edge_weight = torch.tensor([0.9, 0.6, 0.5, 1.0])


def _toy_model() -> LegalGFM:
    return LegalGFM(n_relations=2, n_layers=2, rel_emb_dim=DIM, query_dim=DIM)


def test_model_forward_and_grad():
    g = _ToyGraph()
    m = _toy_model()
    out = m(torch.randn(DIM), torch.tensor([0]), g)
    assert out.shape == (5,), "one score per node"
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0, "scores must be in (0,1)"
    out.sum().backward()
    assert m.query_proj.weight.grad is not None, "gradients must flow (so it can be trained)"
    assert n_params(m) > 100_000


def test_no_seeds_is_safe():
    g = _ToyGraph()
    m = _toy_model()
    out = m(torch.randn(DIM), torch.tensor([], dtype=torch.long), g)
    assert out.shape == (5,)
