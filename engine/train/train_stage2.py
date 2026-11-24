"""Stage-2 supervised GNN training on human-written (query, gold provisions) data.

Stage 1 teaches the shapes of links but leaves the model blind to the question. Stage 2 trains on
real legal questions, each with its gold provisions, so the GNN learns to rank the right provisions
highly for a given question. Warm-starts from gnn_stage1.pt.

Per example: the query is embedded with bge-m3 (the space shared by the GNN and seed retrieval),
`hybrid_seed` supplies the entry points, and the gold provisions are mapped to node indices. The
forward pass is one NBF pass; the loss is multi-positive BCE plus a ranking term (gold scored high,
random negatives low).

Evaluation: the human set is shuffled with a fixed seed (42) and split into 30 test, 30 validation
and the rest for training; the validation and test splits are written to eval/. The checkpoint with
the best validation recall@20 is kept (it is saved as soon as it improves, so an interrupted run
loses nothing). Recall@10, recall@20 and MRR on the test split, which is never used for training or
model selection, are reported at the end.

Run (from engine/):  .venv/bin/python train/train_stage2.py [epochs]
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import random
import sys
import time
from dataclasses import dataclass

import torch

from pythia.db import close_pool, get_pool
from pythia.gnn.checkpoint import save_checkpoint, warm_load_state_dict
from pythia.gnn.model import LegalGFM
from pythia.graph.seed import hybrid_seed
from pythia.graph.tensors import _model as mpnet
from pythia.graph.tensors import load_legal_graph

_DIR = pathlib.Path(__file__).parent
_DATA = _DIR.parent / "eval" / "legal_human.jsonl"
_OUT = _DIR / "gnn_stage2.pt"
_VAL_OUT = _DIR.parent / "eval" / "legal_human_val.jsonl"
_TEST_OUT = _DIR.parent / "eval" / "legal_human_test.jsonl"
N_NEG, ALPHA, ACCUM = 128, 0.3, 4


@dataclass
class Ex:
    qv: torch.Tensor  # [1024] bge-m3 query embedding
    seed_idx: torch.Tensor  # [S] node indices
    gold_idx: torch.Tensor  # [P] node indices
    rid: str


async def _gold_idx(conn, gold_strs: list[str], node_idx: dict[str, int]) -> list[int]:
    out: list[int] = []
    for s in gold_strs:
        ext, label = s.split(" ", 1)
        pid = await conn.fetchval(
            """SELECT p.id::text FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE sd.external_id = $1 AND p.label = $2 LIMIT 1""",
            ext,
            label,
        )
        if pid and pid in node_idx:
            out.append(node_idx[pid])
    return sorted(set(out))


async def _prepare(recs: list[dict], g, dev: str) -> list[Ex]:
    pool = await get_pool()
    queries = [r["query"] for r in recs]
    # sentence-transformers runs .encode() under torch.inference_mode(), and such tensors cannot be
    # saved for backward. .clone() returns ordinary tensors that autograd can use.
    qvs = (
        mpnet()
        .encode(queries, convert_to_tensor=True, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
        .float()
        .clone()
    )
    exs: list[Ex] = []
    async with pool.acquire() as conn:
        for r, qv in zip(recs, qvs):
            gold = await _gold_idx(conn, r["gold"], g.node_idx)
            seeds = await hybrid_seed(r["query"], k=8, jurisdiction=r.get("jurisdiction"), rerank=False)
            sidx = [g.node_idx[s.provision_id] for s in seeds if s.provision_id in g.node_idx]
            if not gold or not sidx:  # no resolvable gold or no seeds: nothing to learn from
                continue
            exs.append(
                Ex(
                    qv.to(dev),
                    torch.tensor(sidx, dtype=torch.long, device=dev),
                    torch.tensor(gold, dtype=torch.long, device=dev),
                    r["id"],
                )
            )
    return exs


def _loss(sc: torch.Tensor, gold_idx: torch.Tensor, dev: str) -> torch.Tensor:
    pos = sc[gold_idx].clamp(1e-6, 1 - 1e-6)
    neg = sc[torch.randint(0, sc.numel(), (N_NEG,), device=dev)].clamp(1e-6, 1 - 1e-6)
    bce = -(torch.log(pos).mean() + torch.log1p(-neg).mean())
    rank = -torch.log(pos.mean() / (pos.mean() + neg.sum() + 1e-6))
    return ALPHA * bce + (1 - ALPHA) * rank


@torch.no_grad()
def _evaluate(model, exs: list[Ex], g) -> dict:
    model.eval()
    r10 = r20 = mrr = 0.0
    for ex in exs:
        sc = model(ex.qv, ex.seed_idx, g).detach()
        ranked = torch.argsort(sc, descending=True).tolist()
        gold = set(ex.gold_idx.tolist())
        r10 += len(set(ranked[:10]) & gold) / len(gold)
        r20 += len(set(ranked[:20]) & gold) / len(gold)
        mrr += next((1.0 / (i + 1) for i, x in enumerate(ranked) if x in gold), 0.0)
    n = max(len(exs), 1)
    return {"R@10": r10 / n, "R@20": r20 / n, "MRR": mrr / n, "n": len(exs)}


async def main(epochs: int = 60) -> None:
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    recs = [json.loads(line) for line in _DATA.read_text().splitlines() if line.strip()]
    random.Random(42).shuffle(recs)
    test_recs, val_recs, train_recs = recs[:30], recs[30:60], recs[60:]
    _VAL_OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in val_recs))
    _TEST_OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in test_recs))

    g = await load_legal_graph(device=dev)
    dim = g.relation_emb.shape[1]  # bge-m3, 1024-d (shared by the GNN and seed retrieval)
    model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
    s1 = _DIR / "gnn_stage1.pt"
    if s1.exists():  # the loader maps a legacy rel_gate onto rel_gate_head
        rep = warm_load_state_dict(model, s1)
        for w in rep["warnings"]:
            print(f"  warm-load warning: {w}")
        print(
            f"warm-started from stage1: {rep['n_transferred']}/{len(model.state_dict())} tensors"
            + (f", reinit {rep['reinit_keys']}" if rep["reinit_keys"] else "")
        )
    try:  # if autograd fails on MPS, fall back to CPU
        model(g.relation_emb[0], g.edge_src[:1], g).sum().backward()
        model.zero_grad()
    except Exception as exc:  # noqa: BLE001
        print(f"MPS autograd failed ({repr(exc)[:40]}…) → CPU")
        dev = "cpu"
        g = await load_legal_graph(device=dev)
        dim = g.relation_emb.shape[1]
        model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
        if s1.exists():  # the rebuilt model must start from stage 1 too, not from random weights
            warm_load_state_dict(model, s1)

    print("preparing examples (encode queries + seed + resolve gold) …")
    train = await _prepare(train_recs, g, dev)
    val = await _prepare(val_recs, g, dev)
    test = await _prepare(test_recs, g, dev)
    print(f"usable: {len(train)} train · {len(val)} val · {len(test)} test  (graph {g.n_nodes:,} nodes)")
    print(f"baseline (warm-started, pre-Stage-2) val: {_evaluate(model, val, g)}")

    opt = torch.optim.AdamW(model.parameters(), lr=3e-4, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    best, best_state, t0 = -1.0, None, time.perf_counter()
    for ep in range(1, epochs + 1):
        model.train()
        random.shuffle(train)
        opt.zero_grad()
        run = 0.0
        acc = 0
        for i, ex in enumerate(train):
            loss = _loss(model(ex.qv, ex.seed_idx, g), ex.gold_idx, dev)
            (loss / ACCUM).backward()
            run += float(loss)
            acc += 1
            if acc == ACCUM or i == len(train) - 1:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
                opt.zero_grad()
                acc = 0
        sched.step()
        if ep % 5 == 0 or ep == 1:
            v = _evaluate(model, val, g)
            star = ""
            if v["R@20"] > best:
                best, best_state, star = v["R@20"], {k: t.cpu().clone() for k, t in model.state_dict().items()}, " *"
                # Save as soon as validation improves, so an interrupted run loses nothing.
                save_checkpoint(_OUT, model, graph=g)
            print(
                f"  ep {ep:3d}  loss {run / len(train):.3f}  val R@10 {v['R@10']:.3f} R@20 {v['R@20']:.3f} "
                f"MRR {v['MRR']:.3f}{star}  ({time.perf_counter() - t0:.0f}s)",
                flush=True,
            )

    if best_state:
        model.load_state_dict(best_state)
    save_checkpoint(_OUT, model, graph=g)
    print(f"\nsaved best (val R@20 {best:.3f}) → {_OUT}")
    print(f"HELD-OUT TEST (raw GNN, never trained/selected on): {_evaluate(model, test, g)}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 60))
