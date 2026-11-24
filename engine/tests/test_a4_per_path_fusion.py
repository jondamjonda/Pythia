"""Track A / A4 — the PER-PATH fusion knob (PythiaQuery.fusion + answer_path_fusion_enabled).

A4 lets the ANSWER path turn on the graph⊕semantic RRF re-rank WITHOUT flipping the single global
`fusion_enabled` (whose contract couples it to the human-gated B3 cutover of the cascade /v1/retrieve
lane). The knob is ADDITIVE and DEFAULT-OFF:

  • PythiaQuery.fusion: bool | None = None  → None means "defer to the global s.fusion_enabled";
  • engine.retrieve effective decision      → q.fusion if q.fusion is not None else s.fusion_enabled;
  • config.answer_path_fusion_enabled=False  → answer() leaves q.fusion=None → byte-identical to today.

These tests are DETERMINISTIC and DB-FREE: the pure floor property test builds NeighborHit objects
directly, and the engine-level tests STUB every NexusPort method so retrieve() runs fully in-memory.
tests/ is exempt from the single-door boundary test, so importing engine/graph internals is allowed.
"""

from __future__ import annotations

import pytest

import pythia.engine as engine_mod
from pythia.config import PythiaSettings, get_settings
from pythia.contract import PythiaQuery
from pythia.engine import PythiaEngine
from pythia.graph.cte_expand import NeighborHit
from pythia.graph.fusion import FusionParams, fuse, graph_rank_score
from pythia.ports import ExpandResult, NexusPort, ProvisionRow, Seed

_P = FusionParams()


# ===========================================================================
# 1. THE FLOOR PROPERTY TEST — the cardinal guarantee under A4.
#    With fusion ON, an ESSENTIAL provision (protected role / PPR-connected) at cosine ~0 is
#    RE-RANKED but NEVER GATED: it survives in the output at/above its structural (graph-seat) rank.
# ===========================================================================
def _hit(pid: str, score: float, relation_path: list[str] | None = None) -> NeighborHit:
    return NeighborHit(pid, hops=1, score=score, relation_path=relation_path or ["references"])


@pytest.mark.parametrize(
    "protect_kwargs, relation_path, roles",
    [
        # a protected ROLE: definition(1) / obligation(2) / prohibition(3) / sanction(7)
        ({}, ["references"], [1]),  # definition role
        ({}, ["references"], [2]),  # obligation role
        ({}, ["references"], [3]),  # prohibition role
        ({}, ["references"], [7]),  # sanction role
        # a protected RELATION (defines) reaching the provision
        ({}, ["defines"], None),
        # the resolved trust rule-kind fold (is_rule) — protected even with a plain relation + no role
        ({"is_rule_by_id": {"E": True}}, ["references"], None),
    ],
)
def test_floor_essential_zero_cosine_never_pruned_or_buried(protect_kwargs, relation_path, roles):
    """E = an ESSENTIAL provision the graph put BEHIND a noise hit, with ZERO cosine (absent from
    sem_scores). N = a non-essential noise hit AHEAD on the graph with the BEST cosine. Fusion must
    RE-RANK but NOT GATE: E survives in the output and never falls below its graph seat."""
    e = _hit("E", score=3.0, relation_path=relation_path)  # essential, WORSE graph rank
    n = _hit("N", score=5.0, relation_path=["references"])  # noise, BETTER graph rank
    sem = {"N": 0.99}  # only N has cosine; E is absent (~0)
    roles_by_id = {"E": roles, "N": None}
    out = fuse([e, n], sem, roles_by_id, method="cte", **protect_kwargs)

    ids = [h.provision_id for h in out]
    # (a) NEVER PRUNED: E is still present in the fused output.
    assert "E" in ids, "an essential provision must never be dropped by fusion"

    e_out = next(h for h in out if h.provision_id == "E")
    # (b) marked protected → the explicit floor is engaged.
    assert e_out.why["protected"] is True

    # (c) NEVER BURIED BELOW ITS GRAPH SEAT: fused >= the graph-only seat at E's graph rank.
    seat = graph_rank_score(e_out.why["graph_rank"], _P)
    assert e_out.fused_score >= seat, (e_out.fused_score, seat)

    # (d) E had no cosine → it sank to the BOTTOM of the semantic lane, yet the floor kept its seat.
    assert e_out.sem_score is None


def test_floor_ppr_connected_zero_cosine_pinned():
    """A PPR-connected provision (ppr_ids member) with a PLAIN relation + no role + zero cosine is
    unconditionally floored to its graph seat — re-ranked, never gated."""
    e = _hit("E", score=1.0, relation_path=["references"])  # not protected by relation/role
    out = fuse([e], {}, {"E": None}, method="ppr", ppr_ids={"E"})
    e_out = out[0]
    assert e_out.why["protected"] is True
    assert e_out.fused_score >= graph_rank_score(0, _P)


# ===========================================================================
# 2. THE EFFECTIVE-DECISION TRUTH TABLE — q.fusion overrides the global, None defers to it.
# ===========================================================================
@pytest.mark.parametrize(
    "q_fusion, global_flag, expected",
    [
        (None, False, False),  # default answer/cascade path → global default-OFF → byte-identical
        (None, True, True),  # cascade lane follows the global flag when the operator flips it (B3)
        (True, False, True),  # answer path fuses WITHOUT touching the global flag
        (False, True, False),  # a caller can force-OFF even when the global is ON
    ],
)
def test_effective_fusion_decision(q_fusion, global_flag, expected):
    """The exact expression engine.retrieve uses: q.fusion if q.fusion is not None else global."""
    q = PythiaQuery(query="x", fusion=q_fusion)
    effective = q.fusion if q.fusion is not None else global_flag
    assert effective is expected


def test_query_fusion_field_default_is_none():
    """The per-path knob ships DEFAULT None → every existing caller defers to the global flag."""
    assert PythiaQuery(query="x").fusion is None


def test_answer_path_flag_defaults_off():
    assert get_settings().answer_path_fusion_enabled is False


# ===========================================================================
# 3. ENGINE-LEVEL, DB-FREE — byte-identical OFF + per-path isolation.
#    All NexusPort methods are stubbed so retrieve() runs fully in-memory and deterministically.
# ===========================================================================
def _stub_nexus(monkeypatch):
    """Stub the NexusPort seed/expand/fetch/semantic methods so PythiaEngine.retrieve runs with no DB.
    Records whether the fusion-only methods (score_semantic / fetch_provisions) were ever called."""
    calls = {"score_semantic": 0, "fetch_provisions": 0}

    async def _search(self, query, *, k=8, jurisdiction=None, as_of=None, rerank=True, rerank_pool=None):
        return [Seed("A", 2.0), Seed("B", 1.0)]

    async def _expand(self, query, seed_ids, *, method="cte", k=20, max_hops=3, as_of=None):
        # one graph-connected neighbour beyond the seeds
        return ExpandResult(hits=[NeighborHit("C", hops=1, score=0.3, relation_path=["references"])])

    def _row(pid: str) -> ProvisionRow:
        return ProvisionRow(
            id=pid,
            label=pid,
            text=f"text {pid}",
            jurisdiction="CZ",
            legal_hierarchy=None,
            valid_from=None,
            valid_to=None,
            source_title="Act",
            external_id="1/2000",
            bindingness=None,
            is_authoritative=None,
            temporal_status=None,
            analysis_roles=None,
        )

    async def _fetch_provisions(self, ids):
        calls["fetch_provisions"] += 1
        return [_row(i) for i in ids]

    async def _score_semantic(self, query, ids):
        calls["score_semantic"] += 1
        return {i: 0.5 for i in ids}

    monkeypatch.setattr(NexusPort, "search", _search, raising=True)
    monkeypatch.setattr(NexusPort, "expand", _expand, raising=True)
    monkeypatch.setattr(NexusPort, "fetch_provisions", _fetch_provisions, raising=True)
    monkeypatch.setattr(NexusPort, "score_semantic", _score_semantic, raising=True)
    return calls


def _force_global_fusion(monkeypatch, enabled: bool) -> None:
    """Bind a fresh settings object (global fusion_enabled=`enabled`, answer flag OFF) into the engine
    module's get_settings so retrieve() sees it. Mirrors test_fusion_parity._force_fusion."""
    s = PythiaSettings()
    s.fusion_enabled = enabled
    monkeypatch.setattr(engine_mod, "get_settings", lambda: s, raising=True)


async def test_off_byte_identical_global_off_and_qfusion_none(monkeypatch):
    """answer_path_fusion_enabled OFF ⇒ q.fusion is None ⇒ engine uses the global fusion_enabled (also
    default-OFF) ⇒ the fusion block is UNREACHABLE: score_semantic / fetch_provisions are never called
    and NO fusion audit key is added — identical to today's structural path."""
    calls = _stub_nexus(monkeypatch)
    _force_global_fusion(monkeypatch, False)

    # q.fusion left at its default None — exactly what answer() passes when the A4 flag is OFF.
    ans = await PythiaEngine().retrieve(PythiaQuery(query="q", k=8), method="cte")

    assert not ans.refused
    assert [c.provision_id for c in ans.cited_provisions]  # structural order returned
    # score_semantic is the fusion-ONLY signal (fetch_provisions also runs in the normal _decorate path,
    # so it is not a fusion discriminator).
    assert calls["score_semantic"] == 0, "OFF path must not score semantics"
    assert "fusion" not in ans.audit_trace
    assert "fusion_degraded" not in ans.audit_trace
    assert all(c.confidence_retrieval is None for c in ans.cited_provisions)


async def test_per_path_isolation_answer_fuses_cascade_does_not(monkeypatch):
    """Per-path isolation with the GLOBAL flag OFF (the B3-gated cascade default):
      • an ANSWER-style query (q.fusion=True) FUSES — audit['fusion'] present, semantics scored;
      • a CASCADE-style query (q.fusion=None) DOES NOT fuse — it follows the OFF global flag.
    Neither call touches the global setting."""
    _force_global_fusion(monkeypatch, False)

    # ANSWER path: the per-leg query answer() builds under answer_path_fusion_enabled.
    calls_a = _stub_nexus(monkeypatch)
    ans = await PythiaEngine().retrieve(PythiaQuery(query="q", k=8, fusion=True), method="cte")
    assert not ans.refused
    assert calls_a["score_semantic"] == 1, "the answer path (q.fusion=True) must fuse"
    assert "fusion" in ans.audit_trace
    assert all(c.confidence_retrieval is not None for c in ans.cited_provisions)

    # CASCADE path: q.fusion left None → follows the OFF global flag → no fusion.
    calls_c = _stub_nexus(monkeypatch)
    casc = await PythiaEngine().retrieve(PythiaQuery(query="q", k=8), method="cte")
    assert not casc.refused
    assert calls_c["score_semantic"] == 0, "the cascade lane (q.fusion=None) must follow the global OFF flag"
    assert "fusion" not in casc.audit_trace
    assert all(c.confidence_retrieval is None for c in casc.cited_provisions)

    # The global flag was never mutated by either call.
    assert get_settings().fusion_enabled is False


async def test_qfusion_false_forces_off_even_when_global_on(monkeypatch):
    """A caller passing q.fusion=False force-disables fusion even when the global flag is ON — proving
    the per-path knob is a full override, not just an OR."""
    calls = _stub_nexus(monkeypatch)
    _force_global_fusion(monkeypatch, True)
    ans = await PythiaEngine().retrieve(PythiaQuery(query="q", k=8, fusion=False), method="cte")
    assert calls["score_semantic"] == 0, "q.fusion=False must override an ON global flag"
    assert "fusion" not in ans.audit_trace
