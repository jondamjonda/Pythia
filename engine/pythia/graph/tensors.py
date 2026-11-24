"""Load the legal graph into the tensors the GNN reads.

Two pieces:
  - Graph structure: provisions (nodes) and typed, weighted, directed edges as PyTorch tensors.
  - Relation embeddings: each relation's one-sentence description embedded with BAAI/bge-m3
    (1024-d), so the model handles any relation by meaning, with no per-relation parameters and
    no retraining.

The GNN encodes queries and relations with the same shared bge-m3 instance that seed retrieval and
the index use (`pythia.ingest.embed_bge.model`), so it reasons in the same 1024-d space as
retrieval.

Only provisions in force at the reference date are loaded; dangling edges (no target in the corpus)
are excluded.

Run:  .venv/bin/python -m pythia.graph.tensors
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import torch

from pythia.db import close_pool, get_pool
from pythia.graph import relations as rel_registry

EMB_DIM = 1024  # bge-m3 dimensionality (shared by the GNN and seed retrieval)


@dataclass
class LegalGraph:
    node_ids: list[str]  # dense index -> provision uuid
    node_idx: dict[str, int]  # provision uuid -> dense index
    n_nodes: int
    edge_src: torch.Tensor  # [E] long: source provision index
    edge_dst: torch.Tensor  # [E] long: target provision index
    edge_rel: torch.Tensor  # [E] long: dense relation index (0..R-1)
    edge_weight: torch.Tensor  # [E] float: w₀·c (legal prior times confidence)
    rel_ids: list[int]  # dense index -> registry rel_id
    rel_labels: list[str]  # dense index -> relation label
    relation_emb: torch.Tensor | None  # [R, 1024] bge-m3 description embeddings (None if not built)
    n_relations: int
    device: str


def _model():
    """Return the GNN's query and relation encoder: the shared bge-m3 instance.

    Reusing the retrieval model keeps the GNN in the same embedding space as retrieval and avoids
    loading a second model.
    """
    from pythia.ingest.embed_bge import model as _bge

    return _bge()


def relation_embeddings(rel_labels: list[str]) -> torch.Tensor:
    """Embed each relation's natural-language description (what makes relations open-vocabulary)."""
    descs = [rel_registry.BY_LABEL[lbl].description for lbl in rel_labels]
    emb = _model().encode(descs, convert_to_tensor=True, normalize_embeddings=True)
    return emb.detach().cpu().float()


async def load_legal_graph(
    as_of: date | None = None, *, device: str = "cpu", with_relation_emb: bool = True
) -> LegalGraph:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            # Exclude annuls/annulled_by (NON_TRAVERSABLE_REL_IDS) by ID, as the CTE, PPR and
            # intelligence queries do, rather than relying on their weight of 0. These edges record
            # erga omnes derogation lineage (a status fact), never a navigation link.
            """SELECT e.src::text AS src, e.dst::text AS dst, e.rel_id, e.weight::float8 AS w
                 FROM pythia_edge e
                 JOIN provision ps ON ps.id = e.src
                 JOIN provision pd ON pd.id = e.dst
                WHERE e.dst IS NOT NULL
                  AND e.rel_id <> ALL($2::int[])
                  AND (ps.valid_to IS NULL OR ps.valid_to > COALESCE($1::date, CURRENT_DATE))
                  AND (pd.valid_to IS NULL OR pd.valid_to > COALESCE($1::date, CURRENT_DATE))
                  AND (ps.valid_from IS NULL OR ps.valid_from <= COALESCE($1::date, CURRENT_DATE))
                  AND (pd.valid_from IS NULL OR pd.valid_from <= COALESCE($1::date, CURRENT_DATE))""",
            as_of,
            list(rel_registry.NON_TRAVERSABLE_REL_IDS),
        )
    node_ids = sorted({r["src"] for r in rows} | {r["dst"] for r in rows})
    node_idx = {u: i for i, u in enumerate(node_ids)}
    rel_present = sorted({r["rel_id"] for r in rows})
    rel_dense = {rid: i for i, rid in enumerate(rel_present)}
    rel_labels = []
    for rid in rel_present:
        try:
            rel_labels.append(rel_registry.BY_ID[rid].label)
        except KeyError:
            raise KeyError(f"unregistered rel_id {rid} — add a row to relations.R0") from None

    edge_src = torch.tensor([node_idx[r["src"]] for r in rows], dtype=torch.long)
    edge_dst = torch.tensor([node_idx[r["dst"]] for r in rows], dtype=torch.long)
    edge_rel = torch.tensor([rel_dense[r["rel_id"]] for r in rows], dtype=torch.long)
    edge_w = torch.tensor([r["w"] for r in rows], dtype=torch.float)

    rel_emb = relation_embeddings(rel_labels) if with_relation_emb else None
    return LegalGraph(
        node_ids=node_ids,
        node_idx=node_idx,
        n_nodes=len(node_ids),
        edge_src=edge_src.to(device),
        edge_dst=edge_dst.to(device),
        edge_rel=edge_rel.to(device),
        edge_weight=edge_w.to(device),
        rel_ids=rel_present,
        rel_labels=rel_labels,
        relation_emb=(rel_emb.to(device) if rel_emb is not None else None),
        n_relations=len(rel_present),
        device=device,
    )


async def _demo() -> None:
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"loading legal graph onto device={dev}  (first run downloads the sentence model ~420MB)…")
    g = await load_legal_graph(device=dev)
    print(f"  nodes (provisions): {g.n_nodes:,}")
    print(f"  edges             : {g.edge_src.numel():,}")
    print(f"  relations         : {g.n_relations}  {g.rel_labels}")
    print(f"  relation_emb shape: {tuple(g.relation_emb.shape)}  (one meaning-vector per relation)")
    ok = (
        int(g.edge_src.max()) < g.n_nodes
        and int(g.edge_dst.max()) < g.n_nodes
        and int(g.edge_rel.max()) < g.n_relations
    )
    print(f"  sanity (indices in range): {ok}")
    print(f"  edge-weight range : [{g.edge_weight.min():.3f}, {g.edge_weight.max():.3f}]")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_demo())
