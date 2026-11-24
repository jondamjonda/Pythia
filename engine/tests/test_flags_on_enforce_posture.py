"""WS5 step 48(g) — VL-E-10 FLAGS-ON pins (the served claim-verify enforce posture).

Two pins carry `@pytest.mark.flags_on` for the dedicated CI sweep (`-m flags_on`), pinning the ON-posture
behaviour the default `-m "not live and not flags_on"` run does NOT exercise:

  • THIS file's pin — the enforce loop, driven end-to-end through `answer()`, stamps the ledger summary with the
    VL-E-10 telemetry surfaces the /metrics block reads: the fix-loop counters (per_status histogram) AND the
    `loop_budget_exceeded` breach signal wired through the loop-budget C9 single source. On a well-grounded
    answer well within the 13-call cap, the breach signal is FALSE (the honest 'no exhaustion' record).
  • the second pin lives in tests/test_structured_claimverify_matrix.py
    (`test_on_on_structured_enforce_is_the_runner_posture`) — the exact structured + claim_verify enforce combo
    the run_aq_all runner enables.

CI RUNS BOTH SWEEPS (see the `flags_on` marker note in pyproject.toml):
    engine default : .venv/bin/python -m pytest tests/ -q -m "not live and not flags_on" -p no:cacheprovider
    engine flags-on: .venv/bin/python -m pytest tests/ -q -m "flags_on"                    -p no:cacheprovider

Test-debt only: no production code changes; reuses the enforce `_harness`.
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings

from tests.test_answer_enforce import _GOOD_DRAFT, _GOOD_FULL, _Q, _harness

pytestmark = pytest.mark.asyncio


@pytest.mark.flags_on
async def test_enforce_posture_stamps_ledger_and_no_budget_breach(monkeypatch):
    """VL-E-10: a well-grounded answer under the ENFORCE posture serves, and its ledger summary carries the
    telemetry surfaces /metrics consumes — the per_status fix-loop histogram AND a NON-breached loop budget
    (loop_budget_exceeded falsy: the answer stayed well within the C9 13-call cap)."""
    _harness(
        monkeypatch,
        PythiaSettings(claim_verify_enabled=True, claim_verify_mode="enforce"),
        draft=_GOOD_DRAFT,
        full_text=_GOOD_FULL,
    )
    res = await ans.answer(_Q, method="cte")
    assert not res.refused
    summary = res.audit_trace["claim_ledger"]["summary"]
    assert summary["mode"] == "enforce"
    assert summary["claims_total"] >= 1
    # the fix-loop histogram (per_status over FINAL_STATUS) is present and totals to claims_total.
    per_status = summary["per_status"]
    assert isinstance(per_status, dict) and sum(per_status.values()) == summary["claims_total"]
    # the loop-budget breach signal is PRESENT-and-false on a well-grounded answer (deterministic, no LLM
    # spend, far under the C9 cap) — never a silent absence that /metrics would read as 0.
    assert "loop_budget_exceeded" in summary and summary["loop_budget_exceeded"] is False
    # …and the fix-loop histogram counters the /metrics block reads are all present on the served posture.
    assert {"rounds", "repaired", "demoted", "safe_dropped", "llm_repairs"} <= set(summary)
