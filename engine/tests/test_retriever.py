"""GNN retriever smoke test — loads model+graph (CPU), returns hits with typed trails.

Plus (S7) the PRODUCTION warm-load path the smoke test misses: a 10-relation-era checkpoint
warm-loaded into the (drifted) 12-relation model must NOT crash, must transfer all 40 backbone
keys bit-identically (torch.equal), and must reinit ONLY the sanctioned rel_gate_head.*. These
are no-DB unit tests over `warm_load_state_dict` directly — they isolate the exact load that
crashed before the rel_gate restructure (a 10-wide gate Parameter into a 12-relation model)."""

from __future__ import annotations

from collections import OrderedDict

import pytest
import torch

from pythia.db import close_pool
from pythia.gnn.checkpoint import FORMAT_TAG, save_checkpoint, warm_load_state_dict
from pythia.gnn.model import LegalGFM
from pythia.graph.seed import sparse_seed
from pythia.gnn.retriever import GNNRetriever

# Production dims: bge-m3 1024-d (the live GNN+seed space). The model is relation-count-free, so
# n_relations is irrelevant to its shape — we build it once and reuse for every warm-load assertion.
_DIM = 1024
# The 40 backbone keys are relation-count-INDEPENDENT; only the legacy rel_gate Parameter (dropped on
# migration) and the new rel_gate_head.* (sanctioned reinit) differ between the eras.
_N_BACKBONE = 40


def _model() -> LegalGFM:
    return LegalGFM(rel_emb_dim=_DIM, query_dim=_DIM)


def _legacy_10_relation_payload(*, wrapped: bool):
    """A checkpoint as it existed in the 10-relation era: the 40 backbone keys from a real model PLUS
    a fixed `rel_gate` Parameter of width 10 (the brittle relation-count param the restructure removed),
    and NO rel_gate_head.*. `wrapped=False` is a BARE legacy OrderedDict (exactly what train/gnn_stage2.pt
    is on disk); `wrapped=True` adds the self-describing v1 envelope with matching embedder meta."""
    src = _model().state_dict()
    sd = OrderedDict(
        (k, v.detach().cpu().clone()) for k, v in src.items() if not k.startswith("rel_gate")
    )  # drop the new head → backbone only
    assert len(sd) == _N_BACKBONE, f"expected {_N_BACKBONE} backbone keys, got {len(sd)}"
    sd["rel_gate"] = torch.ones(10)  # the legacy 10-relation gate Parameter
    if not wrapped:
        return sd
    return {"format": FORMAT_TAG, "state_dict": sd, "meta": {"embedder_id": _embedder_id(), "emb_dim": _DIM}}


def _embedder_id() -> str:
    from pythia.ingest.embed_bge import EMB_MODEL

    return EMB_MODEL


@pytest.mark.asyncio
async def test_gnn_retriever_returns_hits_with_paths():
    r = await GNNRetriever.load(device="cpu")
    seeds = await sparse_seed("investiční fond notifikace povinnost", k=5)
    hits = r.retrieve("investment fund marketing notification", [s.provision_id for s in seeds], k=8)
    assert hits, "retriever returned no hits"
    assert all(0.0 <= h.score <= 1.0 for h in hits)
    assert all(isinstance(h.relation_path, list) for h in hits)
    await close_pool()


def test_warm_load_10_relation_era_into_12_relation_model(tmp_path):
    """The exact load that 500'd before S1: a 10-relation-era checkpoint (rel_gate width 10) warm-loaded
    into the live model (now relation-count-free, formerly a 12-wide gate). Must NOT crash, must transfer
    all 40 backbone keys bit-identically, and must reinit ONLY the sanctioned rel_gate_head.*."""
    legacy = _legacy_10_relation_payload(wrapped=True)
    path = tmp_path / "gnn_stage2.10rel.pt"
    torch.save(legacy, path)

    model = _model()  # fresh build (the production path: build→warm-load)
    report = warm_load_state_dict(model, str(path))  # must not raise (the pre-S1 crash)

    # all 40 backbone keys transferred bit-identically (torch.equal — exact, not approximate)
    assert report["n_transferred"] == _N_BACKBONE
    src_sd = legacy["state_dict"]
    loaded = model.state_dict()
    for k, v in src_sd.items():
        if k.startswith("rel_gate"):
            continue
        assert torch.equal(loaded[k], v), f"backbone key {k} did not warm-load bit-identically"

    # the sanctioned migration: legacy rel_gate dropped, rel_gate_head.* freshly zeros-reinit (gate ≡ 1)
    assert report["dropped_keys"] == ["rel_gate"]
    assert report["reinit_keys"] == ["rel_gate_head.bias", "rel_gate_head.weight"]
    assert float(model.rel_gate_head.weight.detach().abs().sum()) == 0.0
    assert float(model.rel_gate_head.bias.detach().abs().sum()) == 0.0


def test_warm_load_bare_legacy_ordereddict(tmp_path):
    """A BARE legacy OrderedDict (no v1 envelope — exactly train/gnn_stage2.pt's on-disk form) must
    still warm-load: it transfers the 40 backbone keys and WARNS (rather than failing) about the
    unverifiable embedder identity."""
    bare = _legacy_10_relation_payload(wrapped=False)
    assert isinstance(bare, OrderedDict) and "rel_gate" in bare  # genuinely bare + legacy gate
    path = tmp_path / "bare_stage2.pt"
    torch.save(bare, path)

    model = _model()
    report = warm_load_state_dict(model, str(path))  # must not raise
    assert report["n_transferred"] == _N_BACKBONE
    assert report["dropped_keys"] == ["rel_gate"]
    assert report["reinit_keys"] == ["rel_gate_head.bias", "rel_gate_head.weight"]
    assert any("BARE state_dict" in w for w in report["warnings"]), (
        "a bare checkpoint must warn that its embedder identity could not be verified"
    )


def test_warm_load_roundtrip_through_save_checkpoint(tmp_path):
    """A v1 checkpoint written by the project's own save_checkpoint round-trips through the warm loader
    with EVERY tensor bit-identical and ZERO reinit (the new head is already present) — proving the
    production save/load path, not just the legacy-migration path."""
    model = _model()
    # perturb the head off its zeros-init so a faithful round-trip is observable
    with torch.no_grad():
        model.rel_gate_head.weight.normal_()
        model.rel_gate_head.bias.normal_()
    path = tmp_path / "v1.pt"
    save_checkpoint(path, model)

    fresh = _model()
    report = warm_load_state_dict(fresh, str(path))
    assert report["dropped_keys"] == [] and report["reinit_keys"] == []  # nothing migrated/reinit
    for k, v in model.state_dict().items():
        assert torch.equal(fresh.state_dict()[k], v), f"{k} did not round-trip bit-identically"
