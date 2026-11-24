"""VL-G-03 (WS3 step 26) — /metrics claim-loop counters gain the fix-loop histogram + loop_budget_exceeded.

The existing block already exports claims_total / per_status / per_check / per-stage latency; this pins the two
missing signals. They are present (0) on the OFF/shadow path and only accrue from an ENFORCE claim_verify
summary, so the pre-existing keys stay byte-identical.
"""

from __future__ import annotations

from pythia import telemetry


def test_block_always_has_the_new_keys():
    cl = telemetry.metrics()["claim_loop"]
    assert "loop_budget_exceeded" in cl
    assert set(cl["fix_loop"]) == {"rounds", "repaired", "demoted", "safe_dropped", "llm_repairs"}


def test_enforce_summary_accrues_budget_and_fix_loop_counters():
    base = telemetry.metrics()["claim_loop"]
    base_breach = base["loop_budget_exceeded"]
    base_repaired = base["fix_loop"]["repaired"]
    telemetry._record_claim_verify(
        {
            "summary": {
                "claims_total": 3,
                "per_status": {},
                "per_check": {},
                "loop_budget_exceeded": True,
                "rounds": 1,
                "repaired": 2,
                "demoted": 1,
            }
        }
    )
    cl = telemetry.metrics()["claim_loop"]
    assert cl["loop_budget_exceeded"] == base_breach + 1
    assert cl["fix_loop"]["repaired"] == base_repaired + 2
    assert cl["fix_loop"]["rounds"] >= 1 and cl["fix_loop"]["demoted"] >= 1


def test_shadow_summary_does_not_bump_budget():
    base = telemetry.metrics()["claim_loop"]["loop_budget_exceeded"]
    telemetry._record_claim_verify({"summary": {"claims_total": 2, "per_status": {}, "per_check": {}}})
    assert telemetry.metrics()["claim_loop"]["loop_budget_exceeded"] == base  # no breach ⇒ unchanged
