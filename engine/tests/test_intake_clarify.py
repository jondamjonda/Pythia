"""VL-A-08 — the STAGE-2 ENGINE CLARIFY short-circuit. Deterministic (LLM-free via stubs, DB-free via
monkeypatch), all `-m "not live"`.

Covers:
  • CONFIG defaults — committing the two flags is byte-identical (intake_clarify_enabled=False,
    intake_clarify_cos_floor=0.55).
  • BYTE-IDENTICAL OFF — with intake_clarify_enabled OFF the short-circuit is NEVER evaluated: answer()
    proceeds through the metagate/GENERATE stage (the stub GENERATE call fires) even when the intake is
    non-'sufficient' and cosines are low.
  • CLARIFY FIRES — flag ON + a non-'sufficient' intake + a score_semantic stub returning LOW cosines
    ⇒ a render_mode='clarify' PythiaAnswer with drafted_answer=='' and refused=False AND no GENERATE stage
    ran (the stub text() call count is 0).
  • FLOOR RESPECTED — flag ON + non-'sufficient' intake + a HIGH cosine (>= floor) ⇒ NO clarify (answer
    proceeds to GENERATE).
  • SUFFICIENT INTAKE — flag ON + a 'sufficient' intake ⇒ NO clarify regardless of cosine.
  • FAIL-OPEN — score_semantic RAISING ⇒ fail-open ⇒ answer proceeds (no clarify, GENERATE fires).
"""

from __future__ import annotations

import pytest

from pythia.config import get_settings
from pythia.contract import CitedProvision, PythiaAnswer
from pythia.reasoning import answer as answer_mod
from pythia.reasoning.intake import IntakeRecord

_Q = "Beru prvního zaměstnance do své malé restaurace, co musím udělat?"


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Stub LLM — records every generate-stage text() call so a test can prove GENERATE never ran on a clarify.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _StubLLM:
    def __init__(self):
        self.text_calls: list[str | None] = []

    async def json(self, prompt, schema, *, tier="workhorse", stage=None):
        return {}

    async def text(self, prompt, *, system=None, tier="workhorse", stage=None):
        self.text_calls.append(stage)
        return "## Legislativní rámec\nStub answer [262/2006 § 34].\n## Hodnocení\nOK."

    async def available(self):
        return True


def _fake_nexus_factory(*, cosines=None, raises=False):
    """Build a fake NexusPort class whose score_semantic returns `cosines` (id→cos) or raises."""

    class _FakeNexus:
        async def score_semantic(self, query, ids):
            if raises:
                raise RuntimeError("score_semantic boom")
            return dict(cosines or {})

        async def fetch_provisions(self, ids):  # unused on the OFF-metagate/OFF-softlaw path, present for safety
            return []

    return _FakeNexus


def _install_answer_stubs(monkeypatch, *, stub_llm, nexus_cls):
    """Wire answer() LLM-free/DB-free: stub get_llm, _decompose, PythiaEngine.retrieve, _fetch, NexusPort."""
    monkeypatch.setattr(answer_mod, "get_llm", lambda tier: stub_llm)

    async def _fake_decompose(llm, query):
        return (["DECOMP_1", "DECOMP_2"], ["CZ"], "decomp_matter")

    monkeypatch.setattr(answer_mod, "_decompose", _fake_decompose)

    class _FakeEngine:
        async def retrieve(self, q, *, method="cte"):
            cp = CitedProvision(
                provision_id="p1",
                label="§ 34",
                jurisdiction="CZ",
                source_title="Zákoník práce",
                text_md="text",
                score=0.9,
            )
            return PythiaAnswer(refused=False, verdict="ok", cited_provisions=[cp])

    monkeypatch.setattr(answer_mod, "PythiaEngine", _FakeEngine)

    async def _fake_fetch(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, **kw):
        return {
            i: {
                "id": i,
                "label": "§ 34",
                "ext": "262/2006",
                "title": "Zákoník práce",
                "jurisdiction": "CZ",
                "txt": "text",
                "bindingness": None,
                "is_authoritative": True,
                "temporal_status": None,
                "legal_hierarchy": None,
                "pit_status": None,
                "vector_stale": None,
            }
            for i in ids
        }

    monkeypatch.setattr(answer_mod, "_fetch", _fake_fetch)
    monkeypatch.setattr(answer_mod, "NexusPort", nexus_cls)


def _stub_intake(sufficiency):
    async def _run(llm, query, *, profile_context=None):
        return IntakeRecord(
            sub_questions=["INTAKE_A", "INTAKE_B"],
            jurisdiction=["CZ"],
            matter_type="intake_matter",
            sufficiency=sufficiency,
        )

    return _run


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 1. Config defaults — committing this is byte-identical.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_clarify_flag_defaults():
    s = get_settings()
    assert s.intake_clarify_enabled is False
    assert s.intake_clarify_cos_floor == pytest.approx(0.55)


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 2. BYTE-IDENTICAL OFF — the short-circuit is never evaluated; GENERATE fires even with a weak intake.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_off_never_clarifies_even_when_weak(monkeypatch):
    monkeypatch.delenv("PYTHIA_INTAKE_CLARIFY_ENABLED", raising=False)
    monkeypatch.setenv("PYTHIA_INTAKE_V2_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_MODE", "active")
    stub = _StubLLM()
    monkeypatch.setattr(answer_mod, "run_intake", _stub_intake("ambiguous"))
    _install_answer_stubs(monkeypatch, stub_llm=stub, nexus_cls=_fake_nexus_factory(cosines={"p1": 0.01}))

    res = await answer_mod.answer(_Q, verify="off")
    assert res.render_mode != "clarify"  # OFF ⇒ no clarify frame
    assert res.drafted_answer  # GENERATE produced prose
    assert "generate" in stub.text_calls  # the GENERATE stage ran
    assert "intake_clarify" not in res.audit_trace


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 3. CLARIFY FIRES — flag ON + non-'sufficient' intake + LOW cosine ⇒ clarify, ZERO generate.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_on_weak_intake_low_cosine_clarifies_with_zero_generate(monkeypatch):
    monkeypatch.setenv("PYTHIA_INTAKE_CLARIFY_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_MODE", "active")
    stub = _StubLLM()
    monkeypatch.setattr(answer_mod, "run_intake", _stub_intake("ambiguous"))
    _install_answer_stubs(monkeypatch, stub_llm=stub, nexus_cls=_fake_nexus_factory(cosines={"p1": 0.10}))

    res = await answer_mod.answer(_Q, verify="off")
    assert res.render_mode == "clarify"
    assert res.refused is False
    assert res.verdict is None
    assert res.drafted_answer == ""
    # ZERO generate-tier LLM call — the whole point of the pre-GENERATE short-circuit.
    assert stub.text_calls == []
    assert "generate" not in stub.text_calls
    ic = res.audit_trace["intake_clarify"]
    assert ic["sufficiency"] == "ambiguous"
    assert ic["max_cos"] == pytest.approx(0.10)
    assert ic["cos_floor"] == pytest.approx(0.55)
    # the audit trace still carries the standard fragments
    assert res.audit_trace["matter_type"] == "intake_matter"
    assert res.audit_trace["intake"]["sufficiency"] == "ambiguous"


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 4. FLOOR RESPECTED — a HIGH cosine (>= floor) ⇒ NO clarify (answer proceeds to GENERATE).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_on_weak_intake_high_cosine_does_not_clarify(monkeypatch):
    monkeypatch.setenv("PYTHIA_INTAKE_CLARIFY_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_MODE", "active")
    stub = _StubLLM()
    monkeypatch.setattr(answer_mod, "run_intake", _stub_intake("ambiguous"))
    _install_answer_stubs(monkeypatch, stub_llm=stub, nexus_cls=_fake_nexus_factory(cosines={"p1": 0.90}))

    res = await answer_mod.answer(_Q, verify="off")
    assert res.render_mode != "clarify"
    assert res.drafted_answer
    assert "generate" in stub.text_calls


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 5. SUFFICIENT INTAKE — never clarifies regardless of cosine.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_on_sufficient_intake_never_clarifies(monkeypatch):
    monkeypatch.setenv("PYTHIA_INTAKE_CLARIFY_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_MODE", "active")
    stub = _StubLLM()
    monkeypatch.setattr(answer_mod, "run_intake", _stub_intake("sufficient"))
    _install_answer_stubs(monkeypatch, stub_llm=stub, nexus_cls=_fake_nexus_factory(cosines={"p1": 0.01}))

    res = await answer_mod.answer(_Q, verify="off")
    assert res.render_mode != "clarify"
    assert res.drafted_answer
    assert "generate" in stub.text_calls


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# 6. FAIL-OPEN — score_semantic raising ⇒ answer proceeds (no clarify, GENERATE fires).
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
async def test_on_score_semantic_raises_fails_open_no_clarify(monkeypatch):
    monkeypatch.setenv("PYTHIA_INTAKE_CLARIFY_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_ENABLED", "1")
    monkeypatch.setenv("PYTHIA_INTAKE_V2_MODE", "active")
    stub = _StubLLM()
    monkeypatch.setattr(answer_mod, "run_intake", _stub_intake("ambiguous"))
    _install_answer_stubs(monkeypatch, stub_llm=stub, nexus_cls=_fake_nexus_factory(raises=True))

    res = await answer_mod.answer(_Q, verify="off")
    assert res.render_mode != "clarify"  # fail-OPEN ⇒ treated as above the floor
    assert res.drafted_answer
    assert "generate" in stub.text_calls
