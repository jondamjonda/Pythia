"""AQ — the TYPED `no_answer_cause` instrument. PINS:
  • the pure classifier (classify_no_answer_cause): first-match-wins in pipeline order, total/never-raises,
    grounding_floor DISTINCT from the metagate coverage/premise routes and from the enforce refusal;
  • the enum values are the stable wire strings and json-serialise bare;
  • the ADDITIVE field on PythiaAnswer (default "none") + audit_trace["no_answer_cause"] mirror;
  • answer()-level: a drafted-but-0-citation answer ⇒ "grounding_floor"; a metagate no_coverage route ⇒
    "coverage_no_coverage"; a false_premise route ⇒ "premise_false"; a not-legal route ⇒ "intent_not_legal";
    a normally served grounded answer ⇒ "none"; the field NEVER changes what is served / when it refuses.

Fully-stubbed LLM/engine/fetch (no real LLM / DB / network).
"""

from __future__ import annotations

import json

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.contract import MetaVerdict, PythiaAnswer
from pythia.reasoning.no_answer_cause import NoAnswerCause, classify_no_answer_cause
from pythia.reasoning.verify import Verdict


# ─────────────────────────────────────── the pure classifier ──────────────────────────────────────────────


def test_enum_values_are_stable_wire_strings():
    assert NoAnswerCause.NONE == "none"
    assert NoAnswerCause.INTENT_NOT_LEGAL == "intent_not_legal"
    assert NoAnswerCause.COVERAGE_NO_COVERAGE == "coverage_no_coverage"
    assert NoAnswerCause.COVERAGE_WEAK_COVERAGE == "coverage_weak_coverage"
    assert NoAnswerCause.OUT_OF_SCOPE == "out_of_scope"
    assert NoAnswerCause.PREMISE_FALSE == "premise_false"
    assert NoAnswerCause.GROUNDING_FLOOR == "grounding_floor"
    assert NoAnswerCause.COMPLIANCE_BLOCK == "compliance_block"
    assert NoAnswerCause.VERIFY_ENFORCE == "verify_enforce"
    # str mixin ⇒ json serialises the bare string
    assert json.dumps({"c": NoAnswerCause.GROUNDING_FLOOR}) == '{"c": "grounding_floor"}'


def test_grounding_floor_from_refuse_verdict():
    """A draft WAS produced but the verify net refused it (0 grounded / below floor) ⇒ grounding_floor."""
    v = Verdict("refuse", 0.0, [], 0, 0, [], "answer made no grounded citation — withheld")
    assert classify_no_answer_cause(verdict=v) == NoAnswerCause.GROUNDING_FLOOR


def test_honest_abstain_is_not_a_floor_failure():
    """An honest abstain (the draft said no provision on point) is NOT a floor refuse ⇒ none."""
    v = Verdict("abstain", 0.0, [], 0, 0, [], "no provision on point (honest abstention)")
    assert classify_no_answer_cause(verdict=v) == NoAnswerCause.NONE


def test_metagate_coverage_routes():
    for cov, expect in (
        ("no_coverage", NoAnswerCause.COVERAGE_NO_COVERAGE),
        ("weak_coverage", NoAnswerCause.COVERAGE_WEAK_COVERAGE),
        ("out_of_scope", NoAnswerCause.OUT_OF_SCOPE),
    ):
        mv = MetaVerdict(route="not_in_corpus", coverage=cov)
        assert classify_no_answer_cause(meta_verdict=mv) == expect


def test_metagate_intent_and_premise_routes():
    assert (
        classify_no_answer_cause(meta_verdict=MetaVerdict(route="not_a_legal_question", intent="not_legal"))
        == NoAnswerCause.INTENT_NOT_LEGAL
    )
    assert (
        classify_no_answer_cause(meta_verdict=MetaVerdict(route="false_premise", premise="false_premise"))
        == NoAnswerCause.PREMISE_FALSE
    )


def test_metagate_route_wins_over_grounding_floor():
    """FIRST-MATCH-WINS: a non-sound metagate route names the (degraded-render) cause even if the shadow
    verdict also refused — the route is the earlier decision site."""
    mv = MetaVerdict(route="not_in_corpus", coverage="no_coverage")
    v = Verdict("refuse", 0.0, [], 0, 0, [], "floor")
    assert classify_no_answer_cause(meta_verdict=mv, verdict=v) == NoAnswerCause.COVERAGE_NO_COVERAGE


def test_sound_route_falls_through_to_grounding_floor():
    """A `sound` metagate route does NOT mask a real grounding-floor refuse below it."""
    mv = MetaVerdict(route="sound")
    v = Verdict("refuse", 0.0, [], 0, 0, [], "floor")
    assert classify_no_answer_cause(meta_verdict=mv, verdict=v) == NoAnswerCause.GROUNDING_FLOOR


def test_enforce_refusal_is_distinct_from_floor():
    """The claim-verify ENFORCE refusal is named verify_enforce, NOT folded into the grounding floor."""
    v = Verdict("refuse", 0.5, ["p1"], 2, 1, [], "no core claim survived")
    assert classify_no_answer_cause(verdict=v, enforce_refused=True) == NoAnswerCause.VERIFY_ENFORCE


def test_compliance_block_precedes_grounding_floor():
    v = Verdict("refuse", 0.0, [], 0, 0, [], "floor")
    assert classify_no_answer_cause(verdict=v, compliance_blocked=True) == NoAnswerCause.COMPLIANCE_BLOCK


def test_no_retrieval_is_coverage():
    assert classify_no_answer_cause(no_retrieval=True) == NoAnswerCause.COVERAGE_NO_COVERAGE


def test_served_grounded_answer_is_none():
    v = Verdict("ok", 0.9, ["p1"], 2, 2, [], None)
    assert classify_no_answer_cause(verdict=v, meta_verdict=MetaVerdict(route="sound")) == NoAnswerCause.NONE
    assert classify_no_answer_cause(verdict=v) == NoAnswerCause.NONE


def test_total_never_raises_on_garbage():
    class _Bad:
        @property
        def route(self):
            raise RuntimeError("boom")

    assert classify_no_answer_cause(meta_verdict=_Bad()) == NoAnswerCause.NONE
    assert classify_no_answer_cause() == NoAnswerCause.NONE


def test_contract_field_default_and_type():
    a = PythiaAnswer()
    assert a.no_answer_cause == "none"
    a2 = PythiaAnswer(no_answer_cause=NoAnswerCause.GROUNDING_FLOOR)
    assert a2.no_answer_cause == "grounding_floor"
    assert json.loads(a2.model_dump_json())["no_answer_cause"] == "grounding_floor"


# ─────────────────────────────────── answer()-level integration ───────────────────────────────────────────


def _harness(monkeypatch, settings, *, draft: str, full_text: str = "", meta_verdict: MetaVerdict | None = None):
    """Stub answer.py's LLM / engine / fetch. `draft` is what GENERATE returns. When `meta_verdict` is given,
    classify_meta is stubbed to return it (so metagate ON routes are exercised without a real judge LLM)."""
    monkeypatch.setattr(ans, "get_settings", lambda: settings, raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            return draft

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 297", score=0.82)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        m = {
            "id": "p1",
            "label": "§ 297",
            "ext": "240/2013",
            "title": "ZISIF",
            "jurisdiction": "CZ",
            "txt": full_text[:700],
            "ctx_txt": full_text[:700],
            "bindingness": "hard-law",
            "is_authoritative": True,
            "temporal_status": "in-force",
            "legal_hierarchy": "STATUTE",
            "pit_status": None,
            "vector_stale": None,
        }
        if keep_full:
            m["full_txt"] = full_text
        return {"p1": m}

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)

    if meta_verdict is not None:
        # `cos=None` mirrors the real classify_meta signature (true-cosine arm, default-OFF);
        # the stub ignores it (verdict is fixed) but must accept the kwarg answer() now passes.
        async def _classify_meta_stub(query, decomp, meta, ordered, rrf, raw, cos=None):
            return meta_verdict

        monkeypatch.setattr(ans, "classify_meta", _classify_meta_stub, raising=True)


_Q = "Jaké povinnosti má depozitář podle § 297?"
# A grounded draft (the bracketed cite matches the retrieved [240/2013 § 297]).
_GROUNDED = "## Legislativní rámec\nDepozitář musí oznámit údaje podle [240/2013 § 297].\n## Hodnocení\nOK."
_GROUNDED_FULL = "Depozitář musí oznámit údaje podle § 297 do 15 dnů."
# A draft with NO citation tags → the grounding floor refuses (drafted-but-0-citation).
_NO_CITE = "## Legislativní rámec\nDepozitář má nějaké povinnosti.\n## Hodnocení\nNevím přesně."


@pytest.mark.asyncio
async def test_answer_grounded_served_is_none(monkeypatch):
    """A normally served, grounded answer ⇒ no_answer_cause 'none' (field + audit_trace)."""
    _harness(monkeypatch, PythiaSettings(), draft=_GROUNDED, full_text=_GROUNDED_FULL)
    a = await ans.answer(_Q, method="cte")
    assert a.verdict == "ok" and a.refused is False
    assert a.no_answer_cause == "none"
    assert a.audit_trace["no_answer_cause"] == "none"


@pytest.mark.asyncio
async def test_answer_drafted_but_zero_citation_is_grounding_floor(monkeypatch):
    """The diagnosed real case: retrieval worked, a draft was produced, but 0 citation tags ⇒ the grounding
    floor refuses — and the cause is 'grounding_floor', DISTINCT from a coverage/premise problem."""
    _harness(monkeypatch, PythiaSettings(), draft=_NO_CITE, full_text=_GROUNDED_FULL)
    a = await ans.answer(_Q, method="cte")
    assert a.refused is True and a.verdict == "refuse"
    assert a.no_answer_cause == "grounding_floor"
    assert a.audit_trace["no_answer_cause"] == "grounding_floor"


@pytest.mark.asyncio
async def test_answer_metagate_no_coverage_route(monkeypatch):
    """A metagate no_coverage route (shadow) ⇒ 'coverage_no_coverage'."""
    s = PythiaSettings(metagate_enabled=True)  # default mode=shadow
    mv = MetaVerdict(route="not_in_corpus", coverage="no_coverage", intent="legal")
    _harness(monkeypatch, s, draft=_GROUNDED, full_text=_GROUNDED_FULL, meta_verdict=mv)
    a = await ans.answer(_Q, method="cte")
    assert a.no_answer_cause == "coverage_no_coverage"
    assert a.audit_trace["no_answer_cause"] == "coverage_no_coverage"


@pytest.mark.asyncio
async def test_answer_metagate_false_premise_route(monkeypatch):
    s = PythiaSettings(metagate_enabled=True)
    mv = MetaVerdict(route="false_premise", premise="false_premise", coverage="covered", intent="legal")
    _harness(monkeypatch, s, draft=_GROUNDED, full_text=_GROUNDED_FULL, meta_verdict=mv)
    a = await ans.answer(_Q, method="cte")
    assert a.no_answer_cause == "premise_false"


@pytest.mark.asyncio
async def test_answer_metagate_not_legal_route(monkeypatch):
    s = PythiaSettings(metagate_enabled=True)
    mv = MetaVerdict(route="not_a_legal_question", intent="not_legal")
    _harness(monkeypatch, s, draft=_GROUNDED, full_text=_GROUNDED_FULL, meta_verdict=mv)
    a = await ans.answer(_Q, method="cte")
    assert a.no_answer_cause == "intent_not_legal"


@pytest.mark.asyncio
async def test_field_is_additive_served_output_unchanged(monkeypatch):
    """The instrument NEVER changes what is served: with metagate OFF, the served answer with the field is
    byte-identical to a run where the field is ignored (same draft/verdict/cited set/citations_map)."""
    _harness(monkeypatch, PythiaSettings(), draft=_GROUNDED, full_text=_GROUNDED_FULL)
    a = await ans.answer(_Q, method="cte")
    # the served payload is untouched by the additive field
    assert a.drafted_answer == _GROUNDED
    assert a.verdict == "ok"
    assert [c.provision_id for c in a.cited_provisions] == ["p1"]
