"""Stage-2 supervised fine-tune on synthetic data: teach the GNN to follow the question.

Warm-starts from the stage-1 checkpoint and fine-tunes on the synthetic (query, gold) examples
written by gen_stage2.py, so that the query embedding steers the score distribution towards the
relevant provisions. Without this step the GNN ignores the question and highly connected hub
provisions dominate its rankings. Uses the stage-1 loss (ranking plus BCE over sampled negatives)
with a lower learning rate and a few epochs. Saves `train/gnn_stage2.pt`, which `GNNRetriever`
prefers over the stage-1 checkpoint when present.

Run (from engine/, after gen_stage2.py has written train/stage2_data.jsonl):
    .venv/bin/python train/finetune_stage2.py [epochs] [lr] [--resume PATH | --fresh]

Resuming: the backbone always starts from gnn_stage1.pt. By default a re-run then also warm-starts
from this stage's own gnn_stage2.pt if present, so a retrain builds on earlier stage-2 weights.
Pass --resume PATH to warm-start from another checkpoint, or --fresh to use gnn_stage1.pt only.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import random
import sys
import time

import torch

from pythia.db import close_pool
from pythia.gnn.checkpoint import save_checkpoint, warm_load_state_dict
from pythia.gnn.model import LegalGFM
from pythia.graph.tensors import _model as st_model
from pythia.graph.tensors import load_legal_graph

_DATA = pathlib.Path(__file__).parent / "stage2_data.jsonl"
_STAGE1 = pathlib.Path(__file__).parent / "gnn_stage1.pt"
_OUT = pathlib.Path(__file__).parent / "gnn_stage2.pt"
ALPHA = 0.3  # BCE weight; (1-ALPHA) on the ranking term (GFM-RAG)
N_NEG = 256


def _loss(scores: torch.Tensor, gold_idx: torch.Tensor, dev: str):
    neg = torch.randint(0, scores.numel(), (N_NEG,), device=dev)
    pos = scores[gold_idx].clamp(1e-6, 1 - 1e-6)  # [G] gold should be high
    negs = scores[neg].clamp(1e-6, 1 - 1e-6)  # [K] random should be low
    bce = -(torch.log(pos).mean() + torch.log1p(-negs).mean())
    rank = -torch.log(pos.mean() / (pos.mean() + negs.sum() + 1e-6))
    hit = float((pos > negs.max()).float().mean())  # fraction of gold beating the top negative
    return ALPHA * bce + (1 - ALPHA) * rank, hit


def _resolve_resume(resume: str | pathlib.Path | None, fresh: bool) -> pathlib.Path | None:
    """Choose the checkpoint loaded on top of the gnn_stage1.pt backbone, if any.

    --fresh returns None (stage 1 only). An explicit --resume PATH is used as is. Otherwise this
    stage's own output (gnn_stage2.pt) is used when present, so a retrain builds on earlier
    stage-2 weights.
    """
    if fresh:
        return None
    if resume is not None:
        return pathlib.Path(resume)
    return _OUT if _OUT.exists() else None


def _warm_start(model: LegalGFM, src: pathlib.Path | None, *, label: str) -> None:
    """Warm-load `src` into `model` with the supported loader; no-op if src is None or missing."""
    if src is None or not src.exists():
        return
    rep = warm_load_state_dict(model, src)
    for w in rep["warnings"]:
        print(f"  warm-load warning ({label}): {w}")
    print(
        f"{label}: warm-started from {src.name} ({rep['n_transferred']} tensors"
        + (f", reinit {rep['reinit_keys']}" if rep["reinit_keys"] else "")
        + ")"
    )


async def main(
    epochs: int = 4, lr: float = 1e-4, resume: str | pathlib.Path | None = None, fresh: bool = False
) -> None:
    if not _DATA.exists():
        print(f"no {_DATA.name} — run gen_stage2.py first")
        return
    extra = _resolve_resume(resume, fresh)
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    g = await load_legal_graph(device=dev)
    model = LegalGFM(n_relations=g.n_relations).to(dev)
    _warm_start(model, _STAGE1, label="backbone")  # stage-1 weights
    _warm_start(model, extra, label="resume")  # then earlier stage-2 or --resume weights
    try:
        model(g.relation_emb[0], torch.tensor([int(g.edge_src[0])], device=dev), g).sum().backward()
        model.zero_grad()
    except Exception as exc:  # noqa: BLE001
        print(f"autograd on {dev} failed ({repr(exc)[:40]}…) → CPU")
        dev = "cpu"
        g = await load_legal_graph(device=dev)
        model = LegalGFM(n_relations=g.n_relations).to(dev)
        _warm_start(model, _STAGE1, label="backbone")
        _warm_start(model, extra, label="resume")

    rows = [json.loads(ln) for ln in _DATA.read_text().splitlines() if ln.strip()]
    examples = []
    for r in rows:
        gold = [g.node_idx[i] for i in r["gold_ids"] if i in g.node_idx]
        seeds = [g.node_idx[i] for i in r["seed_ids"] if i in g.node_idx]
        if gold and seeds:
            examples.append((r["query"], seeds, gold))
    if not examples:
        print("no usable examples (ids not in graph)")
        await close_pool()
        return
    qvs = st_model().encode([e[0] for e in examples], convert_to_tensor=True, normalize_embeddings=True).float().cpu()
    print(f"Stage-2 · {len(examples)} examples · device {dev} · lr {lr} · {epochs} epochs (warm-start gnn_stage1.pt)")

    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    model.train()
    t0 = time.perf_counter()
    for ep in range(1, epochs + 1):
        order = list(range(len(examples)))
        random.shuffle(order)
        ep_loss = ep_hit = 0.0
        for i in order:
            _, seeds, gold = examples[i]
            qv = qvs[i].to(dev)
            scores = model(qv, torch.tensor(seeds, device=dev), g)
            loss, hit = _loss(scores, torch.tensor(gold, device=dev), dev)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            ep_loss += float(loss)
            ep_hit += hit
        print(
            f"  epoch {ep}/{epochs}  loss {ep_loss / len(order):.3f}  "
            f"gold>neg {ep_hit / len(order):.2f}  ({time.perf_counter() - t0:.0f}s)"
        )

    save_checkpoint(_OUT, model, graph=g)
    print(f"saved → {_OUT}  · total {time.perf_counter() - t0:.0f}s")
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
    eps = int(argv[0]) if len(argv) > 0 else 4
    rate = float(argv[1]) if len(argv) > 1 else 1e-4
    asyncio.run(main(eps, rate, resume=resume_path, fresh=fresh_flag))
