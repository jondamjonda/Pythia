"""Stage-2 GNN training with the human examples augmented by case-law-derived examples.

The same supervised stage 2 as train_stage2.py, but the training pool is the human training split
plus examples mined from 200 real lower-court rulings (query, and gold = provisions the ruling cites
explicitly plus implicitly relevant provisions that passed validation). The human validation and
test splits use the same fixed shuffle (seed 42) as train_stage2.py, so the headline number is
measured on the same independent human benchmark and answers one question: does training on
judicial reasoning improve the GNN on human questions? A held-out case-law test set is reported as
well. Model selection uses human validation recall@20, since human questions are the product target.

Resumable: warm-starts from gnn_stage2_caselaw.pt if present, otherwise from gnn_stage1.pt, and saves
the best checkpoint whenever validation improves. An interrupted run loses nothing, and re-running
the same command continues from the best checkpoint so far.

Run (from engine/):
    PYTHIA_TRAINING_STORE=/path/to/training-store .venv/bin/python train/train_stage2_caselaw.py [epochs]
    (25 epochs by default; re-run to train further)
"""

from __future__ import annotations

import asyncio
import json
import os
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
_HUMAN = _DIR.parent / "eval" / "legal_human.jsonl"
# The mined case-law training store lives outside the repo; point PYTHIA_TRAINING_STORE at it.
_STORE = pathlib.Path(os.environ.get("PYTHIA_TRAINING_STORE", _DIR / "training-store"))
_CL_TRAIN = _STORE / "caselaw_train.jsonl"
_CL_TEST = _STORE / "caselaw_test.jsonl"
_OUT = _DIR / "gnn_stage2_caselaw.pt"
_STAGE1 = _DIR / "gnn_stage1.pt"
_DONE = _DIR / "gnn_stage2_caselaw.progress.json"
N_NEG, ALPHA, ACCUM = 128, 0.3, 4


@dataclass
class Ex:
    qv: torch.Tensor
    seed_idx: torch.Tensor
    gold_idx: torch.Tensor
    rid: str


async def _gold_idx(conn, gold_strs, node_idx):
    out = []
    for s in gold_strs:
        ext, label = s.split(" ", 1)
        pid = await conn.fetchval(
            "SELECT p.id::text FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
            "WHERE sd.external_id=$1 AND p.label=$2 LIMIT 1",
            ext,
            label,
        )
        if pid and pid in node_idx:
            out.append(node_idx[pid])
    return sorted(set(out))


async def _prepare(recs, g, dev):
    pool = await get_pool()
    qvs = (
        mpnet()
        .encode(
            [r["query"] for r in recs],
            convert_to_tensor=True,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=False,
        )
        .float()
        .clone()
    )
    exs = []
    async with pool.acquire() as conn:
        for r, qv in zip(recs, qvs):
            gold = await _gold_idx(conn, r["gold"], g.node_idx)
            seeds = await hybrid_seed(r["query"], k=8, jurisdiction=r.get("jurisdiction"), rerank=False)
            sidx = [g.node_idx[s.provision_id] for s in seeds if s.provision_id in g.node_idx]
            if not gold or not sidx:
                continue
            exs.append(
                Ex(
                    qv.to(dev),
                    torch.tensor(sidx, dtype=torch.long, device=dev),
                    torch.tensor(gold, dtype=torch.long, device=dev),
                    r.get("id", ""),
                )
            )
    return exs


def _loss(sc, gold_idx, dev):
    pos = sc[gold_idx].clamp(1e-6, 1 - 1e-6)
    neg = sc[torch.randint(0, sc.numel(), (N_NEG,), device=dev)].clamp(1e-6, 1 - 1e-6)
    bce = -(torch.log(pos).mean() + torch.log1p(-neg).mean())
    rank = -torch.log(pos.mean() / (pos.mean() + neg.sum() + 1e-6))
    return ALPHA * bce + (1 - ALPHA) * rank


@torch.no_grad()
def _evaluate(model, exs, g):
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
    return {"R@10": round(r10 / n, 3), "R@20": round(r20 / n, 3), "MRR": round(mrr / n, 3), "n": len(exs)}


def _read(p):
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


async def main(epochs=25):
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    human = _read(_HUMAN)
    random.Random(42).shuffle(human)
    human_test, human_val, human_train = human[:30], human[30:60], human[60:]
    cl_train, cl_test = _read(_CL_TRAIN), _read(_CL_TEST)
    train_recs = human_train + cl_train
    print(
        f"data: {len(human_train)} human_train + {len(cl_train)} caselaw_train = {len(train_recs)} train · "
        f"{len(human_val)} human_val · {len(human_test)} human_test · {len(cl_test)} caselaw_test"
    )

    g = await load_legal_graph(device=dev)
    dim = g.relation_emb.shape[1]
    model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
    ckpt = _OUT if _OUT.exists() else _STAGE1
    if ckpt.exists():
        rep = warm_load_state_dict(model, ckpt)
        print(
            f"warm-started from {ckpt.name}: {rep['n_transferred']}/{len(model.state_dict())} tensors"
            + (" (RESUME)" if ckpt == _OUT else "")
        )
    try:
        model(g.relation_emb[0], g.edge_src[:1], g).sum().backward()
        model.zero_grad()
    except Exception as exc:  # noqa: BLE001
        print(f"MPS autograd failed ({repr(exc)[:40]}…) → CPU")
        dev = "cpu"
        g = await load_legal_graph(device=dev)
        dim = g.relation_emb.shape[1]
        model = LegalGFM(n_relations=g.n_relations, rel_emb_dim=dim, query_dim=dim).to(dev)
        if ckpt.exists():
            warm_load_state_dict(model, ckpt)

    print("preparing examples …")
    train = await _prepare(train_recs, g, dev)
    val = await _prepare(human_val, g, dev)
    htest = await _prepare(human_test, g, dev)
    ctest = await _prepare(cl_test, g, dev)
    print(
        f"usable: {len(train)} train · {len(val)} val · {len(htest)} human_test · {len(ctest)} caselaw_test "
        f"(graph {g.n_nodes:,} nodes, {g.n_relations} relations)"
    )
    print(f"baseline val (pre-this-run): {_evaluate(model, val, g)}")

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
                save_checkpoint(_OUT, model, graph=g)
            print(
                f"  ep {ep:3d}  loss {run / max(len(train), 1):.3f}  val R@10 {v['R@10']} R@20 {v['R@20']} "
                f"MRR {v['MRR']}{star}  ({time.perf_counter() - t0:.0f}s)",
                flush=True,
            )

    if best_state:
        model.load_state_dict(best_state)
    save_checkpoint(_OUT, model, graph=g)
    res = {
        "best_val_R@20": round(best, 3),
        "human_test": _evaluate(model, htest, g),
        "caselaw_test": _evaluate(model, ctest, g),
    }
    _DONE.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"\nsaved best (val R@20 {best:.3f}) → {_OUT.name}")
    print(f"HELD-OUT HUMAN TEST: {res['human_test']}")
    print(f"HELD-OUT CASELAW TEST: {res['caselaw_test']}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 25))
