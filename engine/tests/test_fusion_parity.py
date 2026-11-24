"""WP-FUSION Phase A — the default-OFF byte-parity GATE.

Pins the four guarantees of the fusion seam wired into PythiaEngine.retrieve:
  • the flag is OFF by default;
  • with the flag OFF the new code path is genuinely UNREACHABLE (score_semantic AND pythia.engine.fuse
    are monkeypatched to RAISE — if retrieve still succeeds and the audit gained no fusion key, the OFF
    path never touched fusion → it is the exact pre-fusion path);
  • with the flag ON the seam runs (audit_trace["fusion"] present, every hit fused, no candidate dropped);
  • with the flag ON a scorer failure FAILS SOFT back to the structural order (audit_trace["fusion_degraded"]).

DB-backed cases self-skip when `pythia` is unreachable (mirrors test_enrich_definitions). tests/ is exempt
from the single-door boundary test, so importing pythia.engine internals here is allowed.
"""

from __future__ import annotations

import asyncio

import pytest

import pythia.engine as engine_mod
from pythia.config import get_settings
from pythia.contract import PythiaQuery
from pythia.engine import PythiaEngine
from pythia.ports import NexusPort

# A real CZ query that seeds + expands on the live corpus (tax-administration duties).
_QUERY = "povinnosti správce daně"


async def _maybe_pool():
    try:
        from pythia.db import get_pool

        return await asyncio.wait_for(get_pool(), timeout=3.0)
    except Exception:
        return None


def _force_fusion(monkeypatch, enabled: bool) -> None:
    """Make the engine see fusion_enabled=`enabled`. engine.retrieve calls the module-level get_settings()
    afresh on every call, so we patch the name bound in pythia.engine to return a settings object with the
    flag set (mutating a throwaway PythiaSettings() would not reach the engine's own fresh call)."""
    s = get_settings()
    s.fusion_enabled = enabled  # PythiaSettings (pydantic BaseSettings) is not frozen → plain assignment
    monkeypatch.setattr(engine_mod, "get_settings", lambda: s, raising=True)


def test_flag_defaults_off():
    """The fusion flag must ship DEFAULT-OFF (byte-parity is only guaranteed while it is False)."""
    assert get_settings().fusion_enabled is False


async def test_off_path_never_touches_fusion(monkeypatch):
    """With fusion OFF, retrieve must take the EXACT pre-fusion path: it must NOT call score_semantic
    nor pythia.engine.fuse (both patched to raise), and the audit must gain NO fusion key."""
    pool = await _maybe_pool()
    if pool is None:
        pytest.skip("pythia DB not reachable")
    _force_fusion(monkeypatch, False)

    def _boom(*a, **k):
        raise RuntimeError("fusion path reached on the OFF path")

    # If the OFF branch wrongly entered the fusion try-block, EITHER of these would raise; the fail-soft
    # except is INSIDE the enabled branch, so a raise here would surface (the OFF branch has no guard).
    monkeypatch.setattr(NexusPort, "score_semantic", _boom, raising=True)
    monkeypatch.setattr(engine_mod, "fuse", _boom, raising=True)

    ans = await PythiaEngine().retrieve(PythiaQuery(query=_QUERY, k=8), method="cte")
    assert not ans.refused, f"OFF-path retrieve unexpectedly refused: {ans.reason!r}"
    assert ans.cited_provisions, "OFF-path retrieve returned no cited provisions"
    assert "fusion" not in ans.audit_trace, "OFF path must not add a 'fusion' audit key"
    assert "fusion_degraded" not in ans.audit_trace, "OFF path must not add a 'fusion_degraded' key"
    # Byte-parity corollary: with fusion off no provision carries a fused confidence.
    assert all(c.confidence_retrieval is None for c in ans.cited_provisions)
    assert all(c.confidence_breakdown is None for c in ans.cited_provisions)


async def test_on_path_smoke_and_no_drop(monkeypatch):
    """With fusion ON the seam runs: audit_trace['fusion'] carries the per-id breakdown, every returned
    hit has fused_score set, and fusion did NOT shrink the candidate pool — it only re-orders + truncates
    to k (the returned count equals min(k, off-path candidate count))."""
    pool = await _maybe_pool()
    if pool is None:
        pytest.skip("pythia DB not reachable")

    q = PythiaQuery(query=_QUERY, k=8)

    # OFF-path baseline FIRST (default flag) → the candidate pool size fusion must not reduce.
    _force_fusion(monkeypatch, False)
    off = await PythiaEngine().retrieve(q, method="cte")
    if not off.cited_provisions:
        pytest.skip("query seeded/expanded nothing on this corpus")
    off_n = len(off.cited_provisions)

    # ON path.
    _force_fusion(monkeypatch, True)
    on = await PythiaEngine().retrieve(q, method="cte")
    assert not on.refused, f"ON-path retrieve refused: {on.reason!r}"
    assert on.cited_provisions, "ON-path retrieve returned no cited provisions"

    fusion = on.audit_trace.get("fusion")
    assert fusion is not None, "ON path must record an audit_trace['fusion'] block"
    assert "fusion_degraded" not in on.audit_trace, "the clean ON path must not be degraded"
    assert fusion["per_id"], "the fusion audit must carry a per-id breakdown"
    assert all(p.get("fused") is not None for p in fusion["per_id"])

    # Every RETURNED hit was fused (fused_score threaded onto the CitedProvision confidence).
    assert all(c.confidence_retrieval is not None for c in on.cited_provisions), (
        "every fused hit must carry a confidence_retrieval"
    )

    # Fusion only re-orders + truncates to k — it must NOT reduce the candidate pool. Same seeds+expansion
    # feed both runs, so both return min(k, pool); the returned counts must match.
    assert len(on.cited_provisions) == off_n, (
        f"fusion changed the returned count ({len(on.cited_provisions)} vs OFF {off_n})"
    )


async def test_fusion_failsoft(monkeypatch):
    """With fusion ON but the semantic scorer raising, retrieve must FAIL SOFT to the structural order:
    a normal (non-refused) answer with cited provisions, plus audit_trace['fusion_degraded']."""
    pool = await _maybe_pool()
    if pool is None:
        pytest.skip("pythia DB not reachable")
    _force_fusion(monkeypatch, True)

    async def _boom(self, query, ids):
        raise RuntimeError("semantic scorer down")

    monkeypatch.setattr(NexusPort, "score_semantic", _boom, raising=True)

    ans = await PythiaEngine().retrieve(PythiaQuery(query=_QUERY, k=8), method="cte")
    assert not ans.refused, f"fail-soft retrieve unexpectedly refused: {ans.reason!r}"
    assert ans.cited_provisions, "fail-soft retrieve returned no cited provisions"
    assert "fusion_degraded" in ans.audit_trace, "a scorer failure must record audit_trace['fusion_degraded']"
    assert "fusion" not in ans.audit_trace, "the degraded path must not also emit a clean 'fusion' block"
    # Degraded → structural order → no fused confidence threaded.
    assert all(c.confidence_retrieval is None for c in ans.cited_provisions)
