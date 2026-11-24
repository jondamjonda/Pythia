"""VL-B-03/B-04 (WS6 step 50) — failed-leg CONTAINMENT + concurrency cap + per-leg telemetry.

A retrieval leg that RAISES must never 500 the whole answer: it contributes nothing (empty) and is recorded
in audit_trace['leg_errors'], while the surviving legs still ground the answer. The concurrent fan-out is
capped by an answer_leg_concurrency semaphore (RRF is order-independent, so sequential ≡ concurrent output).
The per-leg contribution block audit_trace['legs'] is emitted on the intake-active path (byte-identical off it).
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings

pytestmark = pytest.mark.asyncio

_FULL = "Depozitář musí oznámit údaje podle § 297. " + "X" * 120
_DRAFT = "## Legislativní rámec\nDepozitář musí oznámit údaje podle [240/2013 § 297].\n## Hodnocení\nOK."
_Q = "Jaké povinnosti má depozitář podle § 297?"


def _harness(monkeypatch, settings, *, sub_queries, boom_query=None):
    """Stub answer.py's LLM/engine/fetch. `sub_queries` is what _decompose returns; `boom_query` is the leg
    whose eng.retrieve RAISES (to exercise containment)."""
    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt or stage == "decompose":
                return {"sub_queries": sub_queries, "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return _DRAFT

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            if boom_query is not None and q.query == boom_query:
                raise RuntimeError("leg exploded")
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


async def test_failed_leg_is_contained_not_500(monkeypatch):
    """A raising leg is CONTAINED — the answer is still served from the surviving leg + a leg_errors entry."""
    _harness(monkeypatch, PythiaSettings(), sub_queries=[_Q, "BOOM"], boom_query="BOOM")
    res = await ans.answer(_Q, method="cte")
    assert not res.refused  # surviving leg grounded p1 ⇒ served, not a 500
    errs = res.audit_trace.get("leg_errors")
    assert errs and errs[0]["error"] == "RuntimeError"  # the contained leg is recorded


async def test_sequential_equals_concurrent(monkeypatch):
    """Both legs succeed; concurrent (capped) ≡ sequential served output (RRF is order-independent)."""
    _harness(monkeypatch, PythiaSettings(), sub_queries=[_Q, "second aspect"])
    seq = await ans.answer(_Q, method="cte")
    _harness(
        monkeypatch,
        PythiaSettings(retrieval_concurrent_legs_enabled=True, answer_leg_concurrency=4),
        sub_queries=[_Q, "second aspect"],
    )
    con = await ans.answer(_Q, method="cte")
    assert seq.drafted_answer == con.drafted_answer
    assert seq.citations_map == con.citations_map
    assert [c.model_dump() for c in seq.cited_provisions] == [c.model_dump() for c in con.cited_provisions]


async def test_happy_path_intake_off_has_no_legs_or_errors(monkeypatch):
    """No leg fails + intake OFF ⇒ neither 'legs' nor 'leg_errors' key ⇒ byte-identical audit_trace."""
    _harness(monkeypatch, PythiaSettings(), sub_queries=[_Q])
    res = await ans.answer(_Q, method="cte")
    assert "leg_errors" not in res.audit_trace
    assert "legs" not in res.audit_trace


async def test_legs_block_on_intake_active(monkeypatch):
    """Intake-active ⇒ the per-leg contribution block is emitted (id/source/weight/contribution)."""
    from pythia.reasoning.intake import IntakeRecord

    _harness(monkeypatch, PythiaSettings(intake_v2_enabled=True, intake_v2_mode="active"), sub_queries=[_Q])

    async def _fake_intake(llm, query, *, profile_context=None):
        return IntakeRecord(sub_questions=["aspect A", "aspect B"], jurisdiction=["CZ"], matter_type="employment")

    monkeypatch.setattr(ans, "run_intake", _fake_intake, raising=True)
    res = await ans.answer(_Q, method="cte")
    legs = res.audit_trace.get("legs")
    assert legs is not None and len(legs) >= 1  # legs block present on the intake-active path
    for leg in legs:
        assert {"leg", "source", "weight", "contribution", "n_hits"} <= set(leg)
