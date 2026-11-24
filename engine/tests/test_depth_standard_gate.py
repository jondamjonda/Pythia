"""B1a-HARDEN — the depth-standard's per-family GATE: pure unit tests (lock the gameability fix) + the live
CI gate (Item W: the standard must be a REAL caller, not a one-off report).

The pure tests directly prove the 2026-06-24 audit's #1 fix: a thin/broken new tranche can NO LONGER hide
behind the strong existing corpus, because each dimension gates on the WORST QUALIFYING family slice.

The live test IS the wiring: a `live`-marked assertion that the served `pythia` corpus clears every gated
dimension. The orchestrator's pre-serve gate is:  `.venv/bin/python scripts/corpus_depth_standard.py --json`
(exit 0 iff the standard is MET) — archive the printed JSON as the recorded bar. CI runs this test in the
`live` set; `-m "not live"` keeps the pure gameability tests in the deterministic gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ is not a package — put it on the path so the harness module imports like any other.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import corpus_depth_standard as cds  # noqa: E402


# ───────────────────────────────────────────────────────── pure: the worst-slice gate (the gameability fix)
def test_thin_new_family_fails_on_its_own_slice():
    """The core fix: a registered family at 0% with ≥min_sample CANNOT be averaged green by a strong family."""
    ok, worst, rows = cds._gate_worst({"EU_SECONDARY": (0, 100), "STATUTE": (98, 100)}, 0.70)
    assert ok is False and worst[0] == "EU_SECONDARY" and worst[1] == 0.0


def test_unregistered_new_family_is_gated_by_default():
    """A brand-new legal_hierarchy is GATED by default (safe) — never silently waved through at 0%."""
    ok, worst, rows = cds._gate_worst({"BRAND_NEW_FAMILY": (0, 200)}, 0.70)
    assert ok is False and worst[0] == "BRAND_NEW_FAMILY"


def test_tiny_family_is_reported_not_gated():
    """A family below min_sample is reported, not gated — no single-row noise (the big families carry the gate)."""
    ok, worst, rows = cds._gate_worst({"NEW_SMALL": (0, 5), "STATUTE": (100, 100)}, 0.70)
    assert ok is True and worst[0] == "STATUTE"
    new_small = next(r for r in rows if r["family"] == "NEW_SMALL")
    assert new_small["gated"] is False and "den<" in new_small["note"]


def test_non_section_capable_family_is_not_gated_on_section_dim():
    """SOFT_LAW at 0% section-resolution falls to the doc-floor (report-only), it does not fail the section gate."""
    ok, worst, rows = cds._gate_worst(
        {"SOFT_LAW": (0, 100), "STATUTE": (98, 100), "EU_SECONDARY": (100, 100)}, 0.70, require_section_capable=True
    )
    assert ok is True
    soft = next(r for r in rows if r["family"] == "SOFT_LAW")
    assert soft["gated"] is False and "doc-floor" in soft["note"]


def test_report_only_family_never_gates():
    """CASE_LAW is staged (gated=False) → report-only, never a gate input even at den≥min_sample."""
    ok, worst, rows = cds._gate_worst({"CASE_LAW": (0, 400), "STATUTE": (100, 100)}, 0.70)
    assert ok is True and worst[0] == "STATUTE"
    cl = next(r for r in rows if r["family"] == "CASE_LAW")
    assert cl["gated"] is False and cl["note"] == "report-only"


def test_worst_slice_is_the_gate_when_multiple_qualify():
    """With several qualifying families the GATE is the worst of them, not the average."""
    ok, worst, _ = cds._gate_worst({"A": (90, 100), "B": (60, 100), "C": (99, 100)}, 0.70)
    assert ok is False and worst[0] == "B" and worst[1] == 0.60


# ───────────────────────────────────────────────────────────────────── live: the standard is a REAL gate
@pytest.mark.live
@pytest.mark.asyncio
async def test_served_corpus_meets_the_standard():
    """The served pythia corpus clears every GATED dimension (the pre-serve gate). dim 6 refusal HOLD +
    skipped invariants do not flip the verdict; we skip invariants here (they have their own gates) and
    assert every gated dimension is PASS."""
    res = await cds.run(skip_invariants=True)
    gated_fails = [d["name"] for d in res["dimensions"] if d.get("gated") and not d["ok"]]
    assert not gated_fails, f"depth-standard gated dimensions FAILED: {gated_fails}"
    await cds.close_pool()


# ───────────────────────────────────────────────────── live: the --domain X gate (B1b replicate-and-clear)
@pytest.mark.live
@pytest.mark.asyncio
async def test_replicated_domains_clear_the_standard():
    """B1b: the new domains (employment=labour, real-estate=construction/real-property/civil) clear the depth
    standard on the acts ALREADY present — the proof the standard replicates to a new domain."""
    for dom in ("labour", "construction", "real-property", "civil"):
        dr = await cds.run_domain(dom, skip_invariants=True)
        assert not dr.get("error"), f"{dom}: {dr.get('error')}"
        fails = [e["element"] for e in dr["elements"] if not e["ok"]]
        assert not fails, f"{dom} failed elements: {fails}"
        assert dr["overall"] is True, f"{dom} did not clear the standard"
    await cds.close_pool()


@pytest.mark.live
@pytest.mark.asyncio
async def test_domain_gate_rejects_bogus_domain():
    """A bogus domain (a Nexus sector name / typo) is rejected, NOT silently measured as an empty pass."""
    dr = await cds.run_domain("employment", skip_invariants=True)  # 'employment' is a sector, legal_domain is 'labour'
    assert dr.get("error") and "labour" in dr["known"]
    await cds.close_pool()


def test_refusal_dimension_looks_for_the_app_inside_the_monorepo(monkeypatch):
    """The calibrated refusal floor lives in the app (app/src/regsuper); the lookup must point there."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    cds._dim_refusal()
    assert (Path(sys.path[0]) / "regsuper" / "calibration" / "runtime.py").is_file()
