"""GNN retrieval: score provisions with the model and recover the reasoning trail.

`GNNRetriever` loads the graph and model once, embeds a query, runs one forward pass that scores
every provision, and returns the top k as `NeighborHit`s (the same shape the CTE and PPR expanders
return, so the engine can swap methods freely). For each hit it also recovers why it scored high:
edge importance is the gradient of the score with respect to a per-edge multiplier, and a greedy
walk back from the hit to a seed along the most important edges gives a typed `relation_path`.
Scores and trails are only meaningful with trained weights.

Runs on CPU by default (reliable autograd, and the graph is small); MPS is used for training.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

import torch

from pythia.config import get_settings
from pythia.db import close_pool
from pythia.graph.cte_expand import NeighborHit
from pythia.graph.tensors import EMB_DIM, LegalGraph, _model, load_legal_graph
from pythia.gnn.checkpoint import (
    _is_wrapped,
    _load_obj,
    graph_fingerprint,
    warm_load_state_dict,
)
from pythia.gnn.model import LegalGFM


def _checkpoint_fingerprint(weights_path: str) -> dict | None:
    """Return the graph fingerprint a checkpoint recorded at save time, or None.

    `save_checkpoint` and `migrate_bare_checkpoint` stamp it into `meta['graph_fingerprint']`. A bare
    state dict has no metadata (older training output carried no fingerprint), so it yields None.
    Cheap and read-only: it reloads the on-disk payload and reads one metadata key, with no database
    round-trip and no graph rebuild.
    """
    obj = _load_obj(weights_path)
    if not _is_wrapped(obj):
        return None
    meta = obj.get("meta") or {}
    fp = meta.get("graph_fingerprint")
    return fp if isinstance(fp, dict) else None


def _staleness_warning(weights_path: str, graph: LegalGraph) -> str | None:
    """Return a "checkpoint stale" warning if the live graph differs from the one trained on.

    Compares the live graph fingerprint (nodes and edges) with the one the checkpoint recorded and
    returns a human-readable warning on drift, else None. Pure and read-only: `graph_fingerprint`
    folds the already-loaded in-memory edge tensors, and the trained fingerprint comes from the
    checkpoint's own metadata. A checkpoint without a recorded fingerprint (a bare state dict)
    cannot be compared, so this returns None rather than guess. It never gates loading or changes
    retrieval; the caller only appends the string to `load_warnings`.
    """
    trained = _checkpoint_fingerprint(weights_path)
    if trained is None:
        return None
    live = graph_fingerprint(graph)
    if live is None:
        return None
    if trained != live:
        return f"checkpoint stale: trained fingerprint {trained} != live {live} (retrain recommended)"
    return None


class GNNRetriever:
    """Retrieve provisions by GNN score and explain each hit with a typed relation path."""

    def __init__(
        self,
        graph: LegalGraph,
        model: LegalGFM,
        device: str,
        *,
        untrained: bool = False,
        load_warnings: list[str] | None = None,
    ):
        self.g = graph
        self.model = model.to(device).eval()
        self.device = device
        # Fail-soft flag: True when no usable checkpoint was warm-loaded, either because no weights
        # were supplied or because the checkpoint was incompatible in a way the warm loader cannot
        # bridge (embedder or architecture mismatch). The model still runs on a freshly initialised
        # backbone, but its scores and trails are not meaningful, so the engine reports a `degraded`
        # flag instead of failing with a 500. The flag describes the load outcome only, so callers
        # never confuse a fail-soft fallback with a successfully warm-started checkpoint.
        self.untrained = untrained
        self.load_warnings = list(load_warnings or [])

    @classmethod
    async def load(
        cls,
        *,
        as_of: date | None = None,
        device: str | None = None,
        weights_path: str | None = None,
        strict: bool = False,
    ) -> "GNNRetriever":
        """Build the retriever and warm-load `weights_path` if given.

        By default (`strict=False`) loading is fail-soft: a checkpoint that `warm_load_state_dict`
        refuses (an embedder, architecture or shape mismatch it cannot bridge) yields a retriever
        flagged `untrained=True` instead of raising, so `method=gnn` returns flagged but finite
        results rather than an HTTP 500. The sanctioned `rel_gate` to `rel_gate_head` migration is
        not a failure: every backbone key loads unchanged and `untrained` stays False.

        With `strict=True` (explicit evaluation or reproduction runs) any warm-load failure is
        re-raised unchanged.
        """
        dev = device or "cpu"
        g = await load_legal_graph(as_of=as_of, device=dev)
        # Derive the embedding dimension defensively. An empty graph (no traversable edges, for
        # example an `as_of` after every provision's valid_to, or NON_TRAVERSABLE_REL_IDS removing the
        # last edge) has a 1-D empty relation_emb (torch.Size([0])), so `.shape[1]` would raise
        # IndexError outside the warm-load try/except and fail the request. Fall back to the known
        # embedder dimension so the model can still be built, then degrade gracefully below.
        emb = g.relation_emb
        dim = emb.shape[1] if (emb is not None and emb.ndim == 2 and emb.shape[0] > 0) else EMB_DIM
        model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim)
        if g.n_relations == 0 or g.n_nodes == 0:
            # No traversable graph to reason over: return a flagged but finite retriever (retrieve()
            # returns [] because no seed resolves into the empty node set). method=gnn must never fail
            # with a 500, so degrade here rather than let indexing or the forward pass raise.
            return cls(
                g,
                model,
                dev,
                untrained=True,
                load_warnings=[
                    "empty graph — no traversable edges (e.g. as_of past all provisions); GNN scores not meaningful"
                ],
            )
        if not weights_path:
            # No checkpoint at all: a fresh backbone, degraded by definition (scores are not meaningful).
            return cls(
                g, model, dev, untrained=True, load_warnings=["no checkpoint supplied — running an untrained backbone"]
            )
        try:
            report = warm_load_state_dict(model, weights_path)
            warnings = list(report.get("warnings", []))
            # Optional staleness check (gnn_staleness_check_enabled, off by default). When on, compare
            # the checkpoint's recorded graph fingerprint with the live graph just loaded and, on drift,
            # append a warning that the weights were trained on a different graph (retrain
            # recommended). It only reports: the checkpoint is still warm-loaded and `untrained` stays
            # False.
            if get_settings().gnn_staleness_check_enabled:
                w = _staleness_warning(weights_path, g)
                if w:
                    warnings.append(w)
            return cls(g, model, dev, untrained=False, load_warnings=warnings)
        except Exception as exc:  # noqa: BLE001 — fail-soft is the whole point here
            if strict:
                raise
            # Incompatible checkpoint: rebuild a pristine model (warm_load_state_dict may already have
            # transferred some shape-compatible keys before raising) so we degrade to a clean untrained
            # backbone rather than a half-loaded one, and flag it for the engine.
            model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim)
            return cls(
                g,
                model,
                dev,
                untrained=True,
                load_warnings=[
                    f"incompatible checkpoint {weights_path!r}, falling back to an untrained backbone: {exc}"
                ],
            )

    def _seed_idx(self, seed_ids: list[str]) -> torch.Tensor:
        return torch.tensor(
            [self.g.node_idx[s] for s in seed_ids if s in self.g.node_idx], dtype=torch.long, device=self.device
        )

    def retrieve(self, query: str, seed_ids: list[str], *, k: int = 20, with_paths: bool = True) -> list[NeighborHit]:
        """Return the top `k` non-seed provisions by GNN score, with relation paths if requested."""
        seed_idx = self._seed_idx(seed_ids)
        if seed_idx.numel() == 0:
            return []
        q = _model().encode(query, convert_to_tensor=True, normalize_embeddings=True).float().clone()
        edge_mask = torch.ones(self.g.edge_src.numel(), device=self.device, requires_grad=with_paths)
        scores = self.model(q, seed_idx, self.g, edge_mask=edge_mask)  # [N]
        seedset = {int(i) for i in seed_idx.tolist()}
        order = torch.topk(scores.detach(), min(k + len(seedset), self.g.n_nodes)).indices.tolist()
        hits: list[NeighborHit] = []
        for node in order:
            if node in seedset:
                continue
            path: list[str] = []
            if with_paths:
                grad = torch.autograd.grad(scores[node], edge_mask, retain_graph=True)[0]
                path = self._greedy_path(grad, node, seedset)
            hits.append(
                NeighborHit(
                    self.g.node_ids[node],
                    hops=max(len(path), 1),
                    score=float(scores[node].detach()),
                    relation_path=path,
                )
            )
            if len(hits) >= k:
                break
        return hits

    def _greedy_path(self, edge_grad: torch.Tensor, node: int, seedset: set[int], max_depth: int = 4) -> list[str]:
        """Walk back from `node` to a seed, each step taking the most-important incoming edge."""
        eg = edge_grad.detach().cpu()
        edst, esrc, erel = self.g.edge_dst.cpu(), self.g.edge_src.cpu(), self.g.edge_rel.cpu()
        rels: list[str] = []
        cur, visited = node, {node}
        for _ in range(max_depth):
            if cur in seedset:
                break
            inc = (edst == cur).nonzero(as_tuple=True)[0]
            best, best_imp = -1, float("-inf")
            for e in inc.tolist():
                s = int(esrc[e])
                if s not in visited and float(eg[e]) > best_imp:
                    best_imp, best = float(eg[e]), e
            if best < 0:
                break
            rels.append(self.g.rel_labels[int(erel[best])])
            cur = int(esrc[best])
            visited.add(cur)
        return list(reversed(rels))  # ordered from seed to node


async def _demo(query: str, seed_query: str) -> None:
    from pythia.graph.seed import sparse_seed

    r = await GNNRetriever.load()
    seeds = await sparse_seed(seed_query, k=5)
    hits = r.retrieve(query, [s.provision_id for s in seeds], k=8)
    print(f"device {r.device} · {len(hits)} hits  (UNTRAINED — scores/trails not meaningful yet; plumbing only)")
    for h in hits:
        print(f"  score {h.score:.3f}  hops {h.hops}  trail: {' → '.join(h.relation_path) or '(none)'}")
    await close_pool()


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "marketing notification alternative investment fund Czech Republic"
    sq = sys.argv[2] if len(sys.argv) > 2 else "nabízení investičního fondu notifikace"
    asyncio.run(_demo(q, sq))
