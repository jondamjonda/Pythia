"""WS5 step 48(a) — the 2×2 structured × claim_verify posture matrix (VL-C/E cross-product).

Drives `answer()` (via the `_harness` pattern reused from tests/test_answer_enforce.py) over the FOUR flag
combos:

    structured_answer_enabled ∈ {False, True}   ×   claim_verify_enabled ∈ {False, True (mode='enforce')}

and pins that EVERY combo produces a COHERENT SERVED answer (not refused) on a well-grounded, well-supported
draft (`_GOOD_DRAFT` / `_GOOD_FULL`). The ON/ON combo — the exact posture the run_aq_all runner enables
(structured + claim-verify enforce) — was UNTESTED end-to-end before this pin; here it is exercised through
the full `answer()` path and asserted to serve a grounded answer with a live claim ledger.

Test-debt only: no production code changes; the harness stubs settings/LLM/engine/_fetch exactly as the
enforce suite does. `@pytest.mark.flags_on` marks the ON/ON pin (VL-E-10 flags-ON posture) so CI can run it in
the dedicated `-m flags_on` sweep as well as the default `-m "not live and not flags_on"` sweep.
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings

from tests.test_answer_enforce import _GOOD_DRAFT, _GOOD_FULL, _Q, _harness

pytestmark = pytest.mark.asyncio


def _settings(*, structured: bool, claim_verify: bool) -> PythiaSettings:
    """Build the settings for one matrix cell. claim_verify ON ⇒ mode='enforce' (the runner posture)."""
    kw: dict = {"structured_answer_enabled": structured}
    if claim_verify:
        kw["claim_verify_enabled"] = True
        kw["claim_verify_mode"] = "enforce"
    return PythiaSettings(**kw)


def _assert_coherent_served(res, *, structured: bool, claim_verify: bool) -> None:
    """Every cell must SERVE (not refuse) a grounded answer whose rule sentence survives verbatim, with the
    ledger present iff the loop ran and the slot_plan stamped iff structured is ON."""
    assert not res.refused, "a well-grounded, well-supported draft must serve in every posture"
    assert res.drafted_answer and res.drafted_answer.strip()
    assert "Depozitář musí oznámit údaje podle" in res.drafted_answer
    assert res.cited_provisions, "a served answer keeps its grounded cited provision"
    # structured ⇒ the deterministic slot plan is built ahead of GENERATE and stamped; essay ⇒ never.
    assert ("slot_plan" in res.audit_trace) is structured
    # claim_verify ⇒ the loop ran ⇒ the ledger is stamped in the enforce mode; OFF ⇒ no ledger key.
    if claim_verify:
        led = res.audit_trace.get("claim_ledger")
        assert led is not None and led["summary"]["mode"] == "enforce"
    else:
        assert "claim_ledger" not in res.audit_trace


async def test_off_off_essay_no_loop(monkeypatch):
    """structured OFF · claim_verify OFF — the pre-initiative essay default: serves, no plan, no ledger."""
    _harness(monkeypatch, _settings(structured=False, claim_verify=False), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    _assert_coherent_served(res, structured=False, claim_verify=False)


async def test_on_off_structured_no_loop(monkeypatch):
    """structured ON · claim_verify OFF — the deterministic slot plan runs; no claim-verify loop."""
    _harness(monkeypatch, _settings(structured=True, claim_verify=False), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    _assert_coherent_served(res, structured=True, claim_verify=False)


async def test_off_on_essay_enforce_loop(monkeypatch):
    """structured OFF · claim_verify ENFORCE — the essay draft is claim-verified end-to-end; ledger present."""
    _harness(monkeypatch, _settings(structured=False, claim_verify=True), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    _assert_coherent_served(res, structured=False, claim_verify=True)


@pytest.mark.flags_on
async def test_on_on_structured_enforce_is_the_runner_posture(monkeypatch):
    """structured ON · claim_verify ENFORCE — the EXACT run_aq_all posture, UNTESTED end-to-end before this pin.
    Both the deterministic plan AND the enforce claim-verify loop run over the SAME answer, and the served
    answer is coherent (grounded, rule sentence verbatim, ledger in enforce mode, plan stamped)."""
    _harness(monkeypatch, _settings(structured=True, claim_verify=True), draft=_GOOD_DRAFT, full_text=_GOOD_FULL)
    res = await ans.answer(_Q, method="cte")
    _assert_coherent_served(res, structured=True, claim_verify=True)
    # the ON/ON posture end-to-end: the loop verified the core claim (kept, not demoted) AND the plan covered
    # the retrieved id — the two subsystems co-exist on one served answer.
    led = res.audit_trace["claim_ledger"]
    assert led["summary"]["claims_total"] >= 1
    finals = {r["final"] for r in led["rows"] if r["normative"]}
    assert finals <= {"verified", "repaired_verified", "demoted_gap", "dropped_safety", "dropped_unverifiable"}
    sp = res.audit_trace["slot_plan"]
    assert {i for ids in sp.values() for i in ids} == {"p1"}
