"""Training dry run: prove a stage-2 run would work, without training anything.

Run this before committing to a full training run. It checks, on the live graph and the real
on-disk artifacts, that every training primitive works, by executing each exactly once:

  build     the relation-count-independent `LegalGFM` on the live graph;
  load      warm-load the legacy `train/gnn_stage2.pt` (a bare checkpoint from when the graph had
            10 relations, so its `rel_gate` is 10 wide) through the supported loader
            `pythia.gnn.checkpoint.warm_load_state_dict`. Loading a 10-wide gate into a model with
            more relations used to crash; now the backbone weights transfer unchanged and only
            `rel_gate_head.*` is re-initialised;
  forward   one NBF forward pass scoring every provision, for a single example taken from
            `train/stage2_data.jsonl`;
  loss      one stage-2 style multi-positive BCE plus ranking loss;
  backward  one `loss.backward()` and one `optimizer.step()`.

It then asserts that the step was healthy: the loss and gradient norm are finite, the derived gate
head receives a gradient (`rel_gate_head.weight.grad` is not None, so the inductive gate trains end
to end), and the mean gold score changed after the step (the optimizer had a real effect). On
success it prints 'DRY-RUN OK — training would run'.

It is deliberately not a trainer: there is no epoch loop, only one optimizer step, and the stepped
model is never saved. To train for real, run these yourself (this script does not):

    .venv/bin/python train/pretrain_stage1.py 1500 8     # stage 1: self-supervised pre-training
    .venv/bin/python train/train_stage2.py 60            # stage 2: supervised fine-tune, 60 epochs

Run (from engine/):  .venv/bin/python train/dry_run.py
"""

from __future__ import annotations

import asyncio
import json
import math
import pathlib
import time

import torch

from pythia.db import close_pool
from pythia.gnn.checkpoint import warm_load_state_dict
from pythia.gnn.model import LegalGFM
from pythia.graph.tensors import _model as st_model
from pythia.graph.tensors import load_legal_graph

_DIR = pathlib.Path(__file__).parent
_DATA = _DIR / "stage2_data.jsonl"
_STAGE2 = _DIR / "gnn_stage2.pt"  # legacy bare checkpoint from the 10-relation graph
ALPHA, N_NEG = 0.3, 256  # same loss mix and negatives as finetune_stage2.py


def _loss(scores: torch.Tensor, gold_idx: torch.Tensor, dev: str) -> torch.Tensor:
    """Compute the stage-2 loss (multi-positive BCE plus ranking over sampled negatives).

    Mirrors `finetune_stage2._loss`: pulls gold scores up and random negatives down.
    """
    neg = torch.randint(0, scores.numel(), (N_NEG,), device=dev)
    pos = scores[gold_idx].clamp(1e-6, 1 - 1e-6)  # [G] gold should be high
    negs = scores[neg].clamp(1e-6, 1 - 1e-6)  # [K] random should be low
    bce = -(torch.log(pos).mean() + torch.log1p(-negs).mean())
    rank = -torch.log(pos.mean() / (pos.mean() + negs.sum() + 1e-6))
    return ALPHA * bce + (1 - ALPHA) * rank


def _first_usable_example(rows: list[dict], g) -> tuple[str, list[int], list[int]]:
    """Return the first record whose seeds and gold provisions both resolve to live graph nodes.

    `stage2_data.jsonl` already stores resolved provision UUIDs (seed_ids, gold_ids), so this is a
    plain dictionary lookup with no database round trip.
    """
    for r in rows:
        seeds = [g.node_idx[i] for i in r["seed_ids"] if i in g.node_idx]
        gold = [g.node_idx[i] for i in r["gold_ids"] if i in g.node_idx]
        if seeds and gold:
            return r["query"], seeds, gold
    raise RuntimeError(
        "no usable example in stage2_data.jsonl (no record's seed_ids+gold_ids both resolve into the "
        "live graph) — cannot run the dry-run"
    )


async def main() -> None:
    if not _DATA.exists():
        raise FileNotFoundError(f"missing {_DATA} — run gen_stage2.py first")
    if not _STAGE2.exists():
        raise FileNotFoundError(f"missing {_STAGE2} — the legacy Stage-2 checkpoint the dry-run warm-loads is required")

    # CPU is the reliable autograd path and the dry run is about correctness, not throughput.
    # (The real training scripts use MPS when it is available.)
    dev = "cpu"

    # Build: the relation-count-independent model on the live graph.
    g = await load_legal_graph(device=dev)
    dim = g.relation_emb.shape[1]  # bge-m3, 1024-d (shared by the GNN and seed retrieval)
    model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
    print(
        f"built LegalGFM on live graph: {g.n_nodes:,} nodes · {g.edge_src.numel():,} edges · "
        f"n_relations={g.n_relations} (rel_ids {g.rel_ids if hasattr(g, 'rel_ids') else '?'})"
    )

    # Load: warm-load the legacy 10-relation checkpoint (this load used to crash).
    rep = warm_load_state_dict(model, _STAGE2)
    for w in rep["warnings"]:
        print(f"  warm-load warning: {w}")
    print(
        f"warm-loaded {_STAGE2.name}: {rep['n_transferred']}/{len(model.state_dict())} backbone "
        f"tensors transferred bit-identically; reinit {rep['reinit_keys']}; dropped {rep['dropped_keys']}"
    )

    # A batch of one example from stage2_data.jsonl.
    rows = [json.loads(ln) for ln in _DATA.read_text().splitlines() if ln.strip()]
    query, seeds, gold = _first_usable_example(rows, g)
    qv = st_model().encode(query, convert_to_tensor=True, normalize_embeddings=True).float().clone()
    seed_idx = torch.tensor(seeds, dtype=torch.long, device=dev)
    gold_idx = torch.tensor(gold, dtype=torch.long, device=dev)
    print(f"tiny batch: 1 example · {len(seeds)} seeds · {len(gold)} gold nodes")

    opt = torch.optim.AdamW(model.parameters(), lr=1e-4, weight_decay=1e-4)
    model.train()

    # Gold score before the step, to show afterwards that the optimizer moved it.
    with torch.no_grad():
        score_before = float(model(qv, seed_idx, g)[gold_idx].mean())

    # Exactly one forward pass, loss, backward pass and optimizer step.
    t0 = time.perf_counter()
    opt.zero_grad()
    scores = model(qv, seed_idx, g)  # NBF pass over every provision
    loss = _loss(scores, gold_idx, dev)
    loss.backward()
    grad_norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)  # clip; also a finite check
    gate_grad = model.rel_gate_head.weight.grad  # the derived gate must receive a gradient
    opt.step()
    step_secs = time.perf_counter() - t0

    # Gold score after the step; it must differ from the score before.
    with torch.no_grad():
        score_after = float(model(qv, seed_idx, g)[gold_idx].mean())

    # Assert that the step was healthy.
    loss_val = float(loss.detach())
    gn = float(grad_norm)
    assert math.isfinite(loss_val), f"loss is not finite: {loss_val}"
    assert math.isfinite(gn), f"grad-norm is not finite: {gn}"
    assert gate_grad is not None, (
        "rel_gate_head.weight.grad is None — the derived inductive gate received no gradient (it would not train)"
    )
    assert math.isfinite(float(gate_grad.abs().sum())), "rel_gate_head gradient is not finite"
    assert score_before != score_after, (
        "the gold (target) score did not move after one step — the "
        f"optimizer had no effect (before={score_before}, after={score_after})"
    )

    print(
        f"  one step: loss {loss_val:.4f} · grad-norm {gn:.4f} · "
        f"rel_gate_head.weight.grad |Σ|={float(gate_grad.abs().sum()):.4e} · "
        f"gold score {score_before:.6f} → {score_after:.6f} (Δ={score_after - score_before:+.2e})"
    )
    print(
        f"  per-step cost: {step_secs:.2f}s on {dev}  → a 60-epoch × {len(rows)}-example Stage-2 run "
        f"is ~{step_secs * len(rows) * 60 / 60.0:.0f} min (very rough quote: per-step × examples × epochs)"
    )
    print("DRY-RUN OK — training would run")

    # The stepped model is deliberately not saved.
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
