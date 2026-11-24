"""Stage-1 self-supervised pre-training of the GNN as knowledge-graph completion.

Each sample hides one edge (head, relation, tail) and trains the model to score the true tail above
random negatives. Training details:

- gradient accumulation (several edges per optimizer step, for smoother updates);
- ranking plus BCE loss (alpha = 0.3, as in GFM-RAG); the ranking term handles sparse positives, so
  the loss does not collapse to zero;
- 128 negatives per sample;
- gradient clipping and cosine learning-rate decay;
- a progress signal, hit@1: how often the hidden tail outscores all 128 negatives.

This stage teaches the shapes of links in the graph. Answering legal questions correctly needs
stage 2 (supervised question-to-provision examples). Saves train/gnn_stage1.pt, which
`GNNRetriever` loads, and checkpoints every 300 steps so an interrupted run keeps its progress.

Run (from engine/):
    .venv/bin/python train/pretrain_stage1.py [opt_steps] [accum] [--resume PATH | --fresh]

Resuming: by default a re-run warm-starts from this stage's own gnn_stage1.pt if present, so a
retrain builds on earlier weights instead of discarding them. Pass --resume PATH to warm-start from
another checkpoint, or --fresh to start from a newly initialised model.
"""

from __future__ import annotations

import asyncio
import pathlib
import random
import sys
import time

import torch

from pythia.db import close_pool
from pythia.gnn.checkpoint import save_checkpoint, warm_load_state_dict
from pythia.gnn.model import LegalGFM
from pythia.graph.tensors import load_legal_graph

_OUT = pathlib.Path(__file__).parent / "gnn_stage1.pt"
ALPHA = 0.3  # BCE weight; (1-ALPHA) on the ranking term
N_NEG = 128


def _step_loss(scores: torch.Tensor, tail: int, dev: str):
    neg = torch.randint(0, scores.numel(), (N_NEG,), device=dev)
    pos = scores[tail].clamp(1e-6, 1 - 1e-6)
    negs = scores[neg].clamp(1e-6, 1 - 1e-6)
    bce = -(torch.log(pos) + torch.log1p(-negs).mean())
    rank = -torch.log(pos / (pos + negs.sum() + 1e-6))  # pos should dominate the negatives
    hit = 1.0 if float(pos) > float(negs.max()) else 0.0
    return ALPHA * bce + (1 - ALPHA) * rank, hit


def _resolve_resume(resume: str | pathlib.Path | None, fresh: bool) -> pathlib.Path | None:
    """Choose the checkpoint to warm-start from, or None for a random initialisation.

    --fresh takes precedence. An explicit --resume PATH is used as is. Otherwise this stage's own
    output is used when it exists, so a retrain builds on earlier stage-1 weights.
    """
    if fresh:
        return None
    if resume is not None:
        return pathlib.Path(resume)
    return _OUT if _OUT.exists() else None


def _warm_start(model: LegalGFM, src: pathlib.Path | None) -> None:
    """Warm-load `src` into `model` with the supported loader; no-op if src is None or missing."""
    if src is None or not src.exists():
        print("fresh init (no resume checkpoint)")
        return
    rep = warm_load_state_dict(model, src)
    for w in rep["warnings"]:
        print(f"  warm-load warning: {w}")
    print(
        f"resumed from {src.name}: {rep['n_transferred']} tensors transferred"
        + (f", reinit {rep['reinit_keys']}" if rep["reinit_keys"] else "")
    )


async def main(
    opt_steps: int = 1500, accum: int = 8, resume: str | pathlib.Path | None = None, fresh: bool = False
) -> None:
    src = _resolve_resume(resume, fresh)
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    g = await load_legal_graph(device=dev)
    dim = g.relation_emb.shape[1]  # bge-m3, 1024-d (shared by the GNN and seed retrieval)
    model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
    _warm_start(model, src)
    try:
        model(g.relation_emb[0], torch.tensor([int(g.edge_src[0])], device=dev), g).sum().backward()
        model.zero_grad()
    except Exception as exc:  # noqa: BLE001
        print(f"autograd on {dev} failed ({repr(exc)[:40]}…) → CPU")
        dev = "cpu"
        g = await load_legal_graph(device=dev)
        dim = g.relation_emb.shape[1]
        model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
        _warm_start(model, src)

    opt = torch.optim.AdamW(model.parameters(), lr=5e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, opt_steps)
    n_edges = g.edge_src.numel()
    print(
        f"Stage-1 (sharpened) · device {dev} · {g.n_nodes:,} nodes / {n_edges:,} edges · "
        f"{opt_steps} steps × accum {accum} = {opt_steps * accum:,} samples"
    )
    model.train()
    t0 = time.perf_counter()
    w_loss = w_hit = 0.0
    w_n = 0
    for step in range(1, opt_steps + 1):
        opt.zero_grad()
        step_loss = step_hit = 0.0
        for _ in range(accum):
            e = random.randrange(n_edges)
            head, tail, rel = int(g.edge_src[e]), int(g.edge_dst[e]), int(g.edge_rel[e])
            sc = model(g.relation_emb[rel], torch.tensor([head], device=dev), g)
            loss, hit = _step_loss(sc, tail, dev)
            (loss / accum).backward()
            step_loss += float(loss) / accum
            step_hit += hit / accum
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        sched.step()
        w_loss += step_loss
        w_hit += step_hit
        w_n += 1
        if step == 1 or step % 100 == 0:
            print(
                f"  step {step:4d}/{opt_steps}  loss {w_loss / w_n:.3f}  hit@1 {w_hit / w_n:.2f}  "
                f"lr {sched.get_last_lr()[0]:.1e}  ({time.perf_counter() - t0:.0f}s)",
                flush=True,
            )
            w_loss = w_hit = 0.0
            w_n = 0
        if step % 300 == 0:  # periodic checkpoint, so an interrupted run keeps its progress
            save_checkpoint(_OUT, model, graph=g)

    save_checkpoint(_OUT, model, graph=g)
    print(f"saved → {_OUT}  · total {time.perf_counter() - t0:.0f}s")

    from pythia.graph.seed import sparse_seed
    from pythia.graph.tensors import _model as st

    qv = (
        st()
        .encode(
            "marketing notification alternative investment fund Czech Republic",
            convert_to_tensor=True,
            normalize_embeddings=True,
        )
        .float()
    )
    seeds = await sparse_seed("nabízení investičního fondu notifikace", k=5)
    sidx = torch.tensor([g.node_idx[s.provision_id] for s in seeds if s.provision_id in g.node_idx], device=dev)
    model.eval()
    with torch.no_grad():
        out = model(qv, sidx, g)
    print(
        f"AFTER training · real text query · scores [{float(out.min()):.3f}, {float(out.max()):.3f}] "
        f"std {float(out.std()):.4f}  (untrained was flat ~0.485)"
    )
    await close_pool()


if __name__ == "__main__":
    argv = sys.argv[1:]
    resume_path: str | None = None
    fresh_flag = False
    if "--fresh" in argv:
        fresh_flag = True
        argv.remove("--fresh")
    if "--resume" in argv:
        i = argv.index("--resume")
        if i + 1 >= len(argv):
            raise SystemExit("--resume requires a PATH (usage: --resume PATH)")
        resume_path = argv[i + 1]
        del argv[i : i + 2]
    steps = int(argv[0]) if len(argv) > 0 else 1500
    acc = int(argv[1]) if len(argv) > 1 else 8
    asyncio.run(main(steps, acc, resume=resume_path, fresh=fresh_flag))
