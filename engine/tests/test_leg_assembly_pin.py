"""VL-B-11 (WS6 step 55) — leg-ORDER + WEIGHTS drift PIN for the inline weighted-RRF leg assembly.

The intake-active leg assembly in answer.py is an inline block (no `leg_assembly` knob — see the VL-B-11
decision comment there). This test PINS its order/weights contract so the block cannot silently drift:

  • leg 0 is the RAW query at weight 1.0 (a permanent, additive leg),
  • the intake sub_questions follow as FULL-WEIGHT (1.0) legs, in order,
  • the HyDE legs come AFTER, each at weight `intake_hyde_weight` (a deviation from 0.8, pinned to the
    config default 0.5 — re-evaluated at the step-88 weight sweep).

Driven through `answer()` on the intake-active path with a stubbed intake, asserted against the per-leg
audit block (audit_trace['legs'], which carries leg index → source → weight). Reuses the
test_leg_containment.py harness pattern. No live DB — LLM/engine/fetch are stubbed.
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.reasoning.intake import IntakeRecord

pytestmark = pytest.mark.asyncio

_FULL = "Depozitář musí oznámit údaje podle § 297. " + "X" * 120
_DRAFT = "## Legislativní rámec\nDepozitář musí oznámit údaje podle [240/2013 § 297].\n## Hodnocení\nOK."
_Q = "Jaké povinnosti má depozitář podle § 297?"


def _harness(monkeypatch, settings):
    """Stub answer.py's LLM/engine/fetch. Every leg retrieves the same provision p1 so every leg has hits
    (n_hits > 0) — the pin is about ORDER/WEIGHTS, not about which leg surfaces what."""
    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return _DRAFT

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        return {
            "p1": {
                "id": "p1",
                "label": "§ 297",
                "ext": "240/2013",
                "title": "Z",
                "jurisdiction": "CZ",
                "txt": _FULL[:700],
                "ctx_txt": _FULL[:700],
                "bindingness": "hard-law",
                "is_authoritative": True,
                "temporal_status": "in-force",
                "legal_hierarchy": "STATUTE",
                "pit_status": None,
                "vector_stale": None,
            }
        }

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)


async def test_leg_order_and_weights_pinned_on_intake_active(monkeypatch):
    """Intake-active: leg 0 = raw query @1.0; sub_questions = full-weight legs in order; HyDE legs AFTER at
    intake_hyde_weight. Asserted against audit_trace['legs']."""
    hyde_w = PythiaSettings().intake_hyde_weight  # config default (0.5) — the pinned HyDE weight
    _harness(monkeypatch, PythiaSettings(intake_v2_enabled=True, intake_v2_mode="active"))

    async def _fake_intake(llm, query, *, profile_context=None):
        # 2 sub_questions + 2 HyDE drafts; raw query is NOT among the sub_questions (so leg 0 is additive).
        return IntakeRecord(
            sub_questions=["aspect A", "aspect B"],
            jurisdiction=["CZ"],
            matter_type="finance",
            hyde_drafts=["hyde draft one", "hyde draft two"],
        )

    monkeypatch.setattr(ans, "run_intake", _fake_intake, raising=True)
    res = await ans.answer(_Q, method="cte")

    legs = res.audit_trace.get("legs")
    assert legs is not None
    # 5 legs total: raw query (leg 0) + 2 sub_questions + 2 HyDE drafts.
    assert len(legs) == 5

    # leg 0 is the raw query, a full-weight sub_query leg.
    assert legs[0]["leg"] == 0
    assert legs[0]["source"] == "sub_query"
    assert legs[0]["weight"] == 1.0

    # legs 1..2 are the intake sub_questions, full-weight, in order (source stays sub_query).
    for i in (1, 2):
        assert legs[i]["source"] == "sub_query"
        assert legs[i]["weight"] == 1.0

    # legs 3..4 are the HyDE legs, AFTER the sub_query legs, at intake_hyde_weight.
    for i in (3, 4):
        assert legs[i]["source"] == "hyde"
        assert legs[i]["weight"] == pytest.approx(hyde_w)

    # Contract crosscheck: the LAST sub_query leg precedes the FIRST HyDE leg (order guarantee).
    sub_idxs = [leg["leg"] for leg in legs if leg["source"] == "sub_query"]
    hyde_idxs = [leg["leg"] for leg in legs if leg["source"] == "hyde"]
    assert max(sub_idxs) < min(hyde_idxs)
    # And every HyDE weight is strictly below the full sub_query weight (reduced-weight internal legs).
    assert all(legs[i]["weight"] < 1.0 for i in hyde_idxs)


async def test_hyde_weight_rides_intake_hyde_weight_setting(monkeypatch):
    """The HyDE leg weight is NOT hardcoded — it tracks the intake_hyde_weight setting (proves w=0.5 is a
    knob, re-evaluable at the step-88 sweep, not a frozen constant)."""
    custom_w = 0.7
    _harness(monkeypatch, PythiaSettings(intake_v2_enabled=True, intake_v2_mode="active", intake_hyde_weight=custom_w))

    async def _fake_intake(llm, query, *, profile_context=None):
        return IntakeRecord(
            sub_questions=["aspect A"], jurisdiction=["CZ"], matter_type="finance", hyde_drafts=["hyde draft one"]
        )

    monkeypatch.setattr(ans, "run_intake", _fake_intake, raising=True)
    res = await ans.answer(_Q, method="cte")

    legs = res.audit_trace.get("legs")
    hyde = [leg for leg in legs if leg["source"] == "hyde"]
    assert hyde and all(leg["weight"] == pytest.approx(custom_w) for leg in hyde)


async def test_raw_query_deduped_from_sub_questions(monkeypatch):
    """If intake returns the raw query AS a sub_question, it is NOT duplicated — leg 0 is still the raw query,
    and the duplicate sub_question is dropped (so the additive-leg-0 contract holds)."""
    _harness(monkeypatch, PythiaSettings(intake_v2_enabled=True, intake_v2_mode="active"))

    async def _fake_intake(llm, query, *, profile_context=None):
        # First sub_question IS the raw query — must be deduped so leg 0 stays uniquely additive.
        return IntakeRecord(sub_questions=[_Q, "aspect B"], jurisdiction=["CZ"], matter_type="finance", hyde_drafts=[])

    monkeypatch.setattr(ans, "run_intake", _fake_intake, raising=True)
    res = await ans.answer(_Q, method="cte")

    legs = res.audit_trace.get("legs")
    # raw query (leg 0) + only the ONE non-duplicate sub_question "aspect B" ⇒ 2 legs, no HyDE.
    assert len(legs) == 2
    assert all(leg["source"] == "sub_query" and leg["weight"] == 1.0 for leg in legs)
