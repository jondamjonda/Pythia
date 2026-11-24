"""The GNN model: a query-dependent graph neural retriever over the legal graph.

An inductive GNN in the NBFNet / GFM-RAG lineage, implemented natively so it runs on Apple Silicon
(MPS). One forward pass per query:

1. Labeling trick: seed nodes start with the projected query vector; all other nodes start at zero.
2. L rounds of message passing: each edge sends DistMult(h_src * relation vector), scaled by the
   legal edge weight times a learned per-relation correction, and the messages are summed into the
   target node.
3. Readout: sigmoid(MLP(final state)) gives a relevance score in (0, 1) for every provision.

By the Neural Bellman-Ford identity, this single pass sums over all bounded citation paths at once.
Relations are represented by meaning (embeddings of their text descriptions, or glosses), so the
model is inductive over relation types. Training lives in `engine/train/`.

The per-relation correction (rho) to the legal weight prior is derived from each relation's gloss
embedding by a small head, `rel_gate_head: Linear(hidden_dim -> 1)`, zero-initialised and wrapped as
`1 + tanh(...)`. At initialisation the gate is exactly 1.0, and a relation never seen in training
still gets a finite gate of about 1 from its description alone. The model therefore has no
parameters whose shape depends on the number of relations, so a checkpoint can be warm-started after
the relation set grows. (Older checkpoints used a fixed per-relation parameter vector instead; see
`pythia.gnn.checkpoint` for how they are loaded.)
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F


class LegalGFM(nn.Module):
    """Query-conditioned GNN that scores how relevant every provision is to a query."""

    def __init__(
        self,
        *,
        n_relations: int | None = None,
        rel_emb_dim: int = 1024,
        hidden_dim: int = 512,
        n_layers: int = 4,
        query_dim: int = 1024,
        use_content_features: bool = False,
    ):
        """Build the model.

        Args:
            n_relations: Deprecated and ignored. The model has no relation-count parameters (the
                per-relation gate is derived from gloss embeddings, see the module docstring); the
                argument is kept so existing call sites keep working.
            use_content_features: Reserved extension point for injecting per-node content features
                (for example provision-text embeddings) into the labeling/update path. It is not
                implemented: setting it to True does nothing, so leave it False.
        """
        super().__init__()
        self.use_content_features = use_content_features  # reserved, not implemented
        d = hidden_dim
        self.d = d
        self.n_layers = n_layers
        self.query_proj = nn.Linear(query_dim, d)
        self.rel_proj = nn.Linear(rel_emb_dim, d)
        # g^l: a 2-layer MLP shared across relations, one per layer (the relation transform).
        self.rel_mlp = nn.ModuleList(
            nn.Sequential(nn.Linear(d, d), nn.ReLU(), nn.Linear(d, d)) for _ in range(n_layers)
        )
        # Update: linear on [old_state; aggregated_message], then LayerNorm. The norm keeps the node
        # state bounded across layers so the readout cannot saturate to 0 (training stability).
        self.update = nn.ModuleList(nn.Linear(2 * d, d) for _ in range(n_layers))
        self.norms = nn.ModuleList(nn.LayerNorm(d) for _ in range(n_layers))
        # rho: per-relation correction to the legal weight prior, derived from the relation's gloss
        # embedding rather than stored per relation. Zero init makes the gate exactly 1 at start,
        # and any new relation gets a finite gate zero-shot.
        self.rel_gate_head = nn.Linear(d, 1)
        nn.init.zeros_(self.rel_gate_head.weight)
        nn.init.zeros_(self.rel_gate_head.bias)
        self.readout = nn.Sequential(nn.Linear(d, d // 2), nn.ReLU(), nn.Linear(d // 2, 1))

    def forward(
        self, q_emb: torch.Tensor, seed_idx: torch.Tensor, graph, edge_mask: torch.Tensor | None = None
    ) -> torch.Tensor:
        """Score every node of the graph for one query.

        Args:
            q_emb: Query embedding, shape [query_dim].
            seed_idx: Seed node indices, shape [S].
            graph: The `LegalGraph` tensors.
            edge_mask: Optional per-edge multiplier of ones, shape [E]. Set `requires_grad` on it
                and the gradient of a node's score with respect to it is that edge's importance
                (used to explain which paths drove a score).

        Returns:
            Relevance scores in (0, 1), shape [n_nodes].
        """
        dev = graph.edge_src.device
        n = graph.n_nodes
        q = self.query_proj(q_emb.to(dev))  # [d]
        h = torch.zeros(n, self.d, device=dev)
        if seed_idx.numel() > 0:
            h = h.index_copy(0, seed_idx.to(dev), q.expand(seed_idx.numel(), self.d))
        rel_base = self.rel_proj(graph.relation_emb)  # [R, d]
        # rho derived per relation from its gloss (zero-init head, so tanh(0) = 0 and the gate is 1).
        rel_gate = 1.0 + torch.tanh(self.rel_gate_head(rel_base).squeeze(-1))  # [R] in (0,2)
        esrc, edst, erel = graph.edge_src, graph.edge_dst, graph.edge_rel
        gate = graph.edge_weight * rel_gate[erel]  # [E] legal weight times derived rho
        if edge_mask is not None:
            gate = gate * edge_mask  # per-edge knob for importance attribution
        gate = gate.unsqueeze(-1)  # [E,1]
        # In-degree per target: normalise the aggregation (mean instead of sum) so heavily cited
        # nodes (dense Czech §§) do not blow up message magnitudes and saturate the readout.
        deg = (
            torch.zeros(n, device=dev)
            .index_add(0, edst, torch.ones(edst.numel(), device=dev))
            .clamp_(min=1.0)
            .unsqueeze(-1)
        )  # [n,1]
        for layer in range(self.n_layers):
            rel_l = self.rel_mlp[layer](rel_base)  # [R, d]
            msg = h[esrc] * rel_l[erel] * gate  # [E, d] DistMult message, gated
            agg = torch.zeros(n, self.d, device=dev).index_add(0, edst, msg) / deg  # degree-normalized sum
            h = self.norms[layer](F.relu(self.update[layer](torch.cat([h, agg], dim=-1))))
        return torch.sigmoid(self.readout(h)).squeeze(-1)  # [n] relevance


def n_params(model: nn.Module) -> int:
    """Return the total number of parameters in `model`."""
    return sum(p.numel() for p in model.parameters())
