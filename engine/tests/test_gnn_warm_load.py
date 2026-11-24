"""S4 — the GNN INFERENCE load path (warm-load + fail-soft).

These tests pin the contract that un-breaks `method=gnn`:
  • the on-disk stage2 checkpoint (bare, legacy `rel_gate`=10) warm-loads onto the LIVE graph
    (n_relations drifts, currently 12) and is NOT flagged untrained — the exact RuntimeError that
    500s today no longer fires;
  • a genuinely-incompatible checkpoint (wrong embedder / 768-d mpnet era) FAIL-SOFTS to
    `untrained=True` rather than raising — `method=gnn` degrades instead of 500ing;
  • `strict=True` (explicit eval/repro) RE-RAISES that same incompatibility;
  • the engine surfaces the degraded flag through `reasoning_subgraph["degraded"]` and never 500s.

NO TRAINING: load-only + a single forward (`retrieve`) — no epoch loop, no optimizer, no save.
"""

from __future__ import annotations

import math
import pathlib

import pytest
import torch

from pythia.contract import PythiaQuery
from pythia.db import close_pool
from pythia.engine import PythiaEngine
from pythia.gnn.checkpoint import ARCH_VERSION, FORMAT_TAG
from pythia.gnn.model import LegalGFM
from pythia.gnn.retriever import GNNRetriever
from pythia.graph.seed import sparse_seed

_TRAIN = pathlib.Path(__file__).resolve().parent.parent / "train"
_STAGE2 = _TRAIN / "gnn_stage2.pt"


@pytest.mark.skipif(not _STAGE2.exists(), reason="train/gnn_stage2.pt not present")
@pytest.mark.asyncio
async def test_stage2_checkpoint_warm_loads_on_live_graph():
    # The on-disk stage2 checkpoint warm-loads cleanly onto the live (drifted) graph and is usable.
    # gnn_stage2.pt may be a legacy BARE state_dict (rel_gate=10, gate head reinit to zeros) OR a TRAINED
    # pythia-gnn-ckpt/v1 envelope (gate head trained, non-zero) — BOTH must warm-load: untrained=False, a
    # backbone tensor transfers bit-identically, the derived gate head is present + finite.
    r = await GNNRetriever.load(weights_path=str(_STAGE2))
    assert r.untrained is False, "warm-loading the stage2 backbone must NOT be flagged untrained"
    obj = torch.load(str(_STAGE2), map_location="cpu", weights_only=True)
    disk_sd = obj["state_dict"] if isinstance(obj, dict) and "state_dict" in obj else obj
    assert torch.equal(r.model.state_dict()["rel_proj.weight"].cpu(), disk_sd["rel_proj.weight"])
    assert math.isfinite(float(r.model.rel_gate_head.weight.detach().abs().sum()))
    assert math.isfinite(float(r.model.rel_gate_head.bias.detach().abs().sum()))
    await close_pool()


@pytest.mark.skipif(not _STAGE2.exists(), reason="train/gnn_stage2.pt not present")
@pytest.mark.asyncio
async def test_warm_loaded_retrieve_returns_finite_hits():
    r = await GNNRetriever.load(weights_path=str(_STAGE2))
    seeds = await sparse_seed("investiční fond notifikace povinnost", k=5)
    hits = r.retrieve("investment fund marketing notification", [s.provision_id for s in seeds], k=8)
    assert hits, "retriever returned no hits"
    assert all(math.isfinite(h.score) and 0.0 <= h.score <= 1.0 for h in hits)
    await close_pool()


def _write_incompatible_ckpt(tmp_path, dim: int) -> str:
    """A wrapped checkpoint whose meta claims a DIFFERENT embedder (768-d mpnet) than the live bge-m3 —
    warm_load_state_dict refuses this (different vector space). Weights are a fresh model's, untrained."""
    m = LegalGFM(rel_emb_dim=dim, query_dim=dim)
    sd = {k: v.detach().cpu() for k, v in m.state_dict().items()}
    p = tmp_path / "incompatible.pt"
    torch.save(
        {
            "format": FORMAT_TAG,
            "state_dict": sd,
            "meta": {
                "arch_version": ARCH_VERSION,
                "embedder_id": "sentence-transformers/all-mpnet-base-v2",
                "emb_dim": 768,
            },
        },
        p,
    )
    return str(p)


@pytest.mark.asyncio
async def test_incompatible_checkpoint_failsoft_untrained(tmp_path):
    # First load (no weights) just to read the live embedding dim, then build the incompatible ckpt.
    base = await GNNRetriever.load()
    assert base.untrained is True  # no checkpoint supplied → degraded by definition
    dim = base.g.relation_emb.shape[1]
    bad = _write_incompatible_ckpt(tmp_path, dim)

    r = await GNNRetriever.load(weights_path=bad)  # must NOT raise
    assert r.untrained is True, "an incompatible-embedder checkpoint must fail-soft to untrained"
    assert r.load_warnings, "a fail-soft load must record a warning explaining the fallback"

    with pytest.raises(Exception):  # strict=True re-raises for eval/repro
        await GNNRetriever.load(weights_path=bad, strict=True)
    await close_pool()


# ── B1 — load-time GNN STALENESS GATE (additive, default-OFF, surface-only) ─────────────────────────
# These tests are PURE: no real checkpoint, no torch math, no DB. They monkeypatch the live + trained
# fingerprint primitives and stub the warm-load so the contract is exercised in isolation. The contract:
# with the flag ON a fingerprint MISMATCH appends a 'stale' warning to load_warnings (and ONLY that — the
# checkpoint still warm-loads, untrained stays False, retrieval is untouched); with matching fingerprints
# no warning; with the flag OFF the staleness code path is never entered (byte-identical to today).
from pythia.gnn import retriever as _retr  # noqa: E402


class _FakeGraph:
    """Minimal stand-in for LegalGraph: just enough surface for GNNRetriever.load to pass its
    empty-graph / dim-derivation guards and construct. relation_emb is a real 2-D tensor (dim 4) so the
    dim branch takes the graph value; n_relations/n_nodes are non-zero so the empty-graph early-return is
    skipped. graph_fingerprint() is monkeypatched in the tests, so its edge tensors are never read."""

    def __init__(self) -> None:
        self.relation_emb = torch.zeros(3, 4)
        self.n_relations = 3
        self.n_nodes = 5


class _StubModel:
    """Stands in for a built LegalGFM so load() needs no torch model construction. .to()/.eval() are
    no-ops returning self (GNNRetriever.__init__ calls model.to(device).eval())."""

    def to(self, _device):
        return self

    def eval(self):
        return self


def _install_load_stubs(monkeypatch, *, trained_fp, live_fp, flag: bool):
    """Wire GNNRetriever.load to run with NO graph DB, NO torch model, NO real checkpoint:
    load_legal_graph→_FakeGraph, LegalGFM→_StubModel, warm_load_state_dict→clean report, and the two
    fingerprint readers→fixed dicts. Returns a {'fp_calls': [...]} probe recording every fingerprint-read
    so a test can assert the OFF path NEVER consults the fingerprint."""
    calls: dict[str, list] = {"checkpoint_fp": [], "graph_fp": []}

    async def _fake_load_graph(*_a, **_k):
        return _FakeGraph()

    def _fake_checkpoint_fp(weights_path):
        calls["checkpoint_fp"].append(weights_path)
        return trained_fp

    def _fake_graph_fp(graph):
        calls["graph_fp"].append(graph)
        return live_fp

    class _FakeSettings:
        gnn_staleness_check_enabled = flag

    monkeypatch.setattr(_retr, "load_legal_graph", _fake_load_graph)
    monkeypatch.setattr(_retr, "LegalGFM", lambda *a, **k: _StubModel())
    monkeypatch.setattr(_retr, "warm_load_state_dict", lambda *a, **k: {"warnings": []})
    monkeypatch.setattr(_retr, "_checkpoint_fingerprint", _fake_checkpoint_fp)
    monkeypatch.setattr(_retr, "graph_fingerprint", _fake_graph_fp)
    monkeypatch.setattr(_retr, "get_settings", lambda: _FakeSettings())
    return calls


# unit-level: the pure helper that decides whether to warn.
def test_staleness_warning_on_drift(monkeypatch):
    monkeypatch.setattr(_retr, "_checkpoint_fingerprint", lambda _p: {"n_nodes": 5, "n_edges": 9, "edge_hash": "aaaa"})
    monkeypatch.setattr(_retr, "graph_fingerprint", lambda _g: {"n_nodes": 5, "n_edges": 11, "edge_hash": "bbbb"})
    w = _retr._staleness_warning("ckpt.pt", _FakeGraph())
    assert w is not None and "stale" in w
    assert "retrain recommended" in w


def test_staleness_no_warning_when_fingerprints_match(monkeypatch):
    fp = {"n_nodes": 5, "n_edges": 9, "edge_hash": "aaaa"}
    monkeypatch.setattr(_retr, "_checkpoint_fingerprint", lambda _p: dict(fp))
    monkeypatch.setattr(_retr, "graph_fingerprint", lambda _g: dict(fp))
    assert _retr._staleness_warning("ckpt.pt", _FakeGraph()) is None


def test_staleness_no_warning_when_checkpoint_has_no_fingerprint(monkeypatch):
    # A bare state_dict records no fingerprint → nothing to compare → no (false) drift warning.
    monkeypatch.setattr(_retr, "_checkpoint_fingerprint", lambda _p: None)
    monkeypatch.setattr(_retr, "graph_fingerprint", lambda _g: {"n_nodes": 5, "n_edges": 11, "edge_hash": "bbbb"})
    assert _retr._staleness_warning("ckpt.pt", _FakeGraph()) is None


# load-path: flag ON + drift → a 'stale' warning surfaces (and load behaviour is otherwise unchanged).
@pytest.mark.asyncio
async def test_load_flag_on_drift_appends_stale_warning(monkeypatch):
    calls = _install_load_stubs(
        monkeypatch,
        trained_fp={"n_nodes": 5, "n_edges": 9, "edge_hash": "aaaa"},
        live_fp={"n_nodes": 5, "n_edges": 11, "edge_hash": "bbbb"},
        flag=True,
    )
    r = await GNNRetriever.load(weights_path="ckpt.pt")
    assert r.untrained is False, "staleness is surface-only — it must NOT flag the warm-load untrained"
    stale = [w for w in r.load_warnings if "stale" in w]
    assert len(stale) == 1, f"expected exactly one stale warning, got {r.load_warnings!r}"
    assert calls["checkpoint_fp"] == ["ckpt.pt"] and len(calls["graph_fp"]) == 1


# load-path: flag ON + matching fingerprints → NO stale warning.
@pytest.mark.asyncio
async def test_load_flag_on_match_no_warning(monkeypatch):
    fp = {"n_nodes": 5, "n_edges": 9, "edge_hash": "aaaa"}
    _install_load_stubs(monkeypatch, trained_fp=dict(fp), live_fp=dict(fp), flag=True)
    r = await GNNRetriever.load(weights_path="ckpt.pt")
    assert r.untrained is False
    assert not any("stale" in w for w in r.load_warnings)


# load-path: flag OFF → the staleness code path is NEVER entered. Proves the OFF path is byte-identical:
# load_warnings carries only the warm-loader's own warnings, and the fingerprint primitives are never
# consulted (so retrieval inputs/outputs cannot differ from today).
@pytest.mark.asyncio
async def test_load_flag_off_is_inert(monkeypatch):
    calls = _install_load_stubs(
        monkeypatch,
        trained_fp={"n_nodes": 5, "n_edges": 9, "edge_hash": "aaaa"},
        live_fp={"n_nodes": 5, "n_edges": 11, "edge_hash": "bbbb"},  # a DRIFT that must be IGNORED when OFF
        flag=False,
    )
    r = await GNNRetriever.load(weights_path="ckpt.pt")
    assert r.untrained is False
    assert not any("stale" in w for w in r.load_warnings), "OFF path must not surface staleness"
    assert r.load_warnings == [], "OFF path must leave the warm-loader's warning list untouched"
    assert calls["checkpoint_fp"] == [] and calls["graph_fp"] == [], (
        "OFF path must NOT read any fingerprint (byte-identical to pre-B1 behaviour)"
    )


@pytest.mark.skipif(not _STAGE2.exists(), reason="train/gnn_stage2.pt not present")
@pytest.mark.asyncio
async def test_engine_method_gnn_never_500s():
    # End-to-end engine path: with the warm-loadable stage2 checkpoint, method=gnn returns (not 500),
    # not flagged degraded. (This is the engine path that raises a 500 today.)
    ans = await PythiaEngine().retrieve(
        PythiaQuery(query="nabízení alternativního investičního fondu notifikace", k=10),
        method="gnn",
    )
    assert ans.reasoning_subgraph.get("method") == "gnn"
    assert not ans.reasoning_subgraph.get("degraded", False)
    await close_pool()
