"""Track B / B1b — the 7-slot structured answer: slot_verify grounding net + prompt-selector byte-identity.

Fast, PURE unit tests (no LLM, no DB, no engine). They pin the two load-bearing guarantees:
  • byte-identical OFF — with structured_answer_enabled OFF the gen-prompt selector returns the exact
    essay-shape prompts (the structured 7-slot templates are never emitted);
  • the slot_verify anti-fabrication net — an ungrounded CLAIM slot is demoted to an honest gap line, and
    the 'Vaše situace' restatement is guarded against an uncited legal characterization (C13).
"""

from __future__ import annotations

import pytest

import pythia.reasoning.answer as ans
from pythia.config import PythiaSettings
from pythia.contract import PracticalBlock, Section
from pythia.reasoning.answer import (
    _GEN_ABSENT,
    _GEN_PROMPT,
    _GEN_PROMPT_STRICT,
    _GEN_PROMPT_STRICT_TEMPORAL,
    _GEN_PROMPT_STRUCTURED,
    _GEN_PROMPT_STRUCTURED_STRICT,
    _GEN_PROMPT_STRUCTURED_STRICT_TEMPORAL,
    _GEN_PROMPT_STRUCTURED_TEMPORAL,
    _GEN_PROMPT_TEMPORAL,
    _gen_facts_block,
    _gen_profile_block,
    _gen_prompts,
)
from pythia.reasoning.intake import IntakeFact, IntakeRecord
from pythia.reasoning.sections import split_into_sections
from pythia.reasoning.slot_verify import verify_slots

_STRUCTURED_KINDS = {"situation", "applies", "obligations", "steps", "where", "risks", "gaps"}


def _sec(kind: str, text: str, chips: list[dict] | None = None) -> Section:
    return Section(kind=kind, heading=kind, text=text, citations_map=chips or [])


# ── prompt selector: byte-identical OFF, 7-slot ON ──────────────────────────────────────────────────
def test_gen_prompts_off_is_the_essay_prompt():
    gen, strict = _gen_prompts(False, structured=False)
    assert gen == _GEN_PROMPT  # exact essay-shape prompt, byte-identical to today
    assert "Vaše situace" not in gen  # no structured slot leaks into the OFF path
    assert isinstance(strict, str) and strict


def test_gen_prompts_on_selects_the_seven_slot_template():
    gen, _ = _gen_prompts(False, structured=True)
    assert gen == _GEN_PROMPT_STRUCTURED
    assert "Vaše situace" in gen and "Kam se obrátit" in gen


def test_essay_draft_never_emits_structured_kinds():
    # The module-level _HEADINGS is SHARED by both shapes; a real essay-shape draft (produced on the
    # structured-OFF path) must still split into ONLY the three essay kinds — the structured heading rows
    # match none of its headings, so no 7-slot kind can leak into the default path.
    draft = "## Legislativní rámec\nRelevantní ustanovení jsou §§ 1 a 2.\n## Hodnocení\nZ výše uvedeného plyne závěr.\n"
    kinds = {sec.kind for sec in split_into_sections(draft, [], [])}
    assert kinds & _STRUCTURED_KINDS == set()
    assert kinds <= {"legislative", "case_law", "assessment"}


# ── slot_verify: claim-slot grounding ───────────────────────────────────────────────────────────────
def test_claim_slot_without_grounded_chip_is_demoted_with_a_gap():
    sections = [_sec("where", "## Kam se obrátit\nObraťte se na finanční úřad.", chips=[])]
    block = verify_slots(sections)
    assert isinstance(block, PracticalBlock)
    assert "where" in block.demoted_slots
    assert any(g["gap_class"] == "authority_locator" for g in block.gaps)
    assert all(g["status"] == "DRAFT/advocate-review-pending" for g in block.gaps)


def test_claim_slot_with_a_grounded_chip_is_not_demoted():
    chip = [{"start": 0, "end": 3, "provision_id": "p1", "label": "§ 1", "corpus": "235/2004"}]
    sections = [_sec("obligations", "## Konkrétní povinnosti a lhůty\n§ 1 …", chips=chip)]
    block = verify_slots(sections)
    assert block.demoted_slots == []
    assert block.gaps == []


def test_gaps_slot_is_never_checked():
    # 'Co korpus nepokrývá' is expected-uncited — an empty chip list must NOT demote it.
    sections = [_sec("gaps", "## Co korpus nepokrývá\nNěkteré praktické kroky korpus nepokrývá.", chips=[])]
    block = verify_slots(sections)
    assert block.demoted_slots == []


# ── slot_verify: the situation-slot guard (C13) ─────────────────────────────────────────────────────
def test_situation_slot_with_a_section_reference_is_flagged():
    sections = [_sec("situation", "## Vaše situace\nJako s.r.o. jste plátce DPH podle § 6.")]
    block = verify_slots(sections)
    assert "situation" in block.demoted_slots
    assert any(g["gap_class"] == "situation_uncited_legal_claim" for g in block.gaps)


def test_situation_slot_with_an_obligation_modal_is_flagged():
    sections = [_sec("situation", "## Vaše situace\nMusíte se zaregistrovat.")]
    block = verify_slots(sections)
    assert "situation" in block.demoted_slots


def test_clean_situation_restatement_is_not_flagged():
    sections = [_sec("situation", "## Vaše situace\nChcete přijmout prvního zaměstnance v Plzni.")]
    block = verify_slots(sections)
    assert block.demoted_slots == []
    assert block.gaps == []


# ── slot_verify: verbatim practical items + purity ──────────────────────────────────────────────────
def test_practical_items_are_lifted_verbatim_from_cited_text():
    text = "Přihlášku je nutné podat do 8 dnů ode dne nástupu."
    block = verify_slots(
        [
            _sec(
                "obligations",
                "## Konkrétní povinnosti a lhůty\n…",
                chips=[{"start": 0, "end": 1, "provision_id": "187/2006 § 93"}],
            )
        ],
        cited_texts={"187/2006 § 93": text},
    )
    assert block.items, "a 'do 8 dnů' deadline should be extracted into a PracticalItem"
    it = next(i for i in block.items if i.fact_type == "deadline")
    assert it.value_raw in text  # VERBATIM excerpt, never transformed
    assert it.provision_id == "187/2006 § 93"
    assert it.slot == "obligations"


def test_empty_sections_yield_an_empty_block_no_raise():
    block = verify_slots([], cited_texts=None)
    assert block == PracticalBlock()
    assert block.profile_used is False


def test_profile_used_flag_is_carried_through():
    block = verify_slots([], profile_used=True)
    assert block.profile_used is True


# ── B1b — the {profile}/{facts} DATA-block wiring: the LATENT KeyError regression guard ───────────────
# The 7-slot templates carry {profile} + {facts} placeholders; the two generate .format() calls in answer()
# historically supplied only q/ctx[/tags], so flipping structured_answer_enabled ON crashed the generate step
# with `KeyError: 'profile'`. These pin (a) the templates format cleanly with the FULL kwarg set answer() now
# passes, (b) the essay templates stay byte-identical when the extra kwargs ride along, and (c) the real
# answer() generate + strict-retry both format without raising when the flag is ON.
_ALL_STRUCTURED = (
    _GEN_PROMPT_STRUCTURED,
    _GEN_PROMPT_STRUCTURED_STRICT,
    _GEN_PROMPT_STRUCTURED_TEMPORAL,
    _GEN_PROMPT_STRUCTURED_STRICT_TEMPORAL,
)
_ALL_ESSAY = (
    _GEN_PROMPT,
    _GEN_PROMPT_STRICT,
    _GEN_PROMPT_TEMPORAL,
    _GEN_PROMPT_STRICT_TEMPORAL,
)


def test_every_structured_template_formats_with_the_full_kwargs():
    # The exact kwargs answer() now passes into the structured .format calls — none may KeyError, and the
    # {profile}/{facts} content must land in the rendered prompt.
    for tmpl in _ALL_STRUCTURED:
        out = tmpl.format(q="Q", ctx="CTX", tags="TAGS", profile="PROF", facts="FCT")
        assert "PROF" in out and "FCT" in out
        assert "Vaše situace" in out  # the 7-slot shape actually rendered


def test_structured_template_without_profile_kwarg_is_the_reproduced_crash():
    # Documents the ORIGINAL bug: the structured template referenced {profile}/{facts}, so the old q/ctx-only
    # .format raised. If someone drops the fields from the template this test flips and flags it.
    with pytest.raises(KeyError):
        _GEN_PROMPT_STRUCTURED.format(q="Q", ctx="CTX")


def test_wall0_profile_fence_and_honest_degradation():
    """VL-C-06 (WS5 step 44): the PROFILE block fences the facts as DATA-not-instructions when a profile is
    present, and emits an HONEST-DEGRADATION line (never a silent blank) when none is."""
    from pythia.reasoning.answer import _WALL0_PROFILE_FENCE, _WALL0_PROFILE_NONE, _gen_profile_block

    with_facts = _gen_profile_block({"legal_form": "sro"}, None)
    assert _WALL0_PROFILE_FENCE in with_facts and "sro" in with_facts  # fenced as data, not instructions
    none = _gen_profile_block(None, None)
    assert none == _WALL0_PROFILE_NONE and "none provided" in none  # honest degradation, not blank


def test_essay_templates_ignore_the_extra_profile_facts_kwargs_byte_identical():
    # str.format silently drops unreferenced kwargs, so the essay path is byte-identical when answer() passes
    # the extra profile=/facts= along on the structured-OFF path.
    for tmpl in _ALL_ESSAY:
        base = tmpl.format(q="Q", ctx="CTX", tags="TAGS")
        extra = tmpl.format(q="Q", ctx="CTX", tags="TAGS", profile="PROF", facts="FCT")
        assert base == extra
        assert "Vaše situace" not in extra  # no structured slot leaks into the essay path


# ── _gen_profile_block / _gen_facts_block — PURE block builders ───────────────────────────────────────
def test_profile_block_absent_is_the_honest_degradation_line():
    # VL-C-06 (WS5 step 44): no profile ⇒ the WALL-0 honest-degradation line (not a silent blank placeholder).
    from pythia.reasoning.answer import _WALL0_PROFILE_NONE

    assert _gen_profile_block(None, None) == _WALL0_PROFILE_NONE
    assert _gen_profile_block({}, None) == _WALL0_PROFILE_NONE


def test_profile_block_prefers_validated_intake_facts():
    rec = IntakeRecord(
        sub_questions=["q"],
        jurisdiction=["CZ"],
        matter_type="employment",
        facts=[
            IntakeFact(field="legal_form", value="s.r.o.", evidence_quote="s.r.o."),
            IntakeFact(field="sector", value="  gastro  ", evidence_quote="gastro"),
        ],
    )
    out = _gen_profile_block(None, rec)
    assert "legal_form: s.r.o." in out
    assert "sector: gastro" in out  # value whitespace-folded via _norm


def test_profile_block_falls_back_to_project_context_when_intake_absent():
    # intake OFF (intake_rec None) but profile-context supplied ⇒ render the whitelisted project_context.
    out = _gen_profile_block({"legal_form": "s.r.o.", "is_employer": True}, None)
    assert "legal_form: s.r.o." in out
    assert "is_employer: yes" in out


def test_facts_block_lifts_verbatim_excerpts_tagged_by_provision():
    meta = {"p1": {"ext": "187/2006", "label": "§ 93", "txt": "Přihlášku je nutné podat do 8 dnů ode dne nástupu."}}
    out = _gen_facts_block(meta, ["p1"])
    assert out.startswith("[187/2006 § 93]")
    assert "deadline:" in out
    assert "do 8 dnů" in out  # the verbatim statutory span, never transformed


def test_facts_block_absent_is_the_benign_placeholder():
    meta = {"p1": {"ext": "1/2000", "label": "§ 1", "txt": "Obecné ustanovení bez praktického faktu."}}
    assert _gen_facts_block(meta, ["p1"]) == _GEN_ABSENT


# ── answer() end-to-end: the flag ON must NOT KeyError at the generate step ───────────────────────────
def _structured_harness(monkeypatch, *, draft, prompts=None, gen_calls=None):
    """Stub answer.py's settings/LLM/engine/fetch so answer() runs the real generate + strict-retry .format
    with structured_answer_enabled ON, NO DB, NO LLM. `prompts` collects every generate prompt string."""
    monkeypatch.setattr(ans, "get_settings", lambda: PythiaSettings(structured_answer_enabled=True), raising=True)

    class _LLM:
        async def available(self):
            return True

        async def json(self, prompt, schema, *, tier="cheap", stage=None):
            if "planning step" in prompt:  # _decompose
                return {"sub_queries": ["q"], "jurisdiction": ["CZ"], "matter_type": "x"}
            return {"unsupported": []}  # faithfulness

        async def text(self, prompt, *, system=None, tier="generate", stage=None):
            if prompts is not None:
                prompts.append(prompt)
            if gen_calls is not None:
                gen_calls[stage] = gen_calls.get(stage, 0) + 1
            return draft

    monkeypatch.setattr(ans, "get_llm", lambda tier: _LLM(), raising=True)

    class _Eng:
        async def retrieve(self, q, *, method="cte"):
            from pythia.contract import CitedProvision as _CP

            return type("R", (), {"cited_provisions": [_CP(provision_id="p1", label="§ 93", score=0.8)]})()

    monkeypatch.setattr(ans, "PythiaEngine", lambda: _Eng(), raising=True)

    async def _fetch_stub(ids, *, as_of=None, render_asof=False, ctx_window=False, terms=None, keep_full=False):
        txt = "Přihlášku je nutné podat do 8 dnů ode dne nástupu."
        return {
            "p1": {
                "id": "p1",
                "label": "§ 93",
                "ext": "187/2006",
                "title": "ZNP",
                "jurisdiction": "CZ",
                "txt": txt,
                "ctx_txt": txt,
                "bindingness": "hard-law",
                "is_authoritative": True,
                "temporal_status": "in-force",
                "legal_hierarchy": "STATUTE",
                "pit_status": None,
                "vector_stale": None,
            }
        }

    monkeypatch.setattr(ans, "_fetch", _fetch_stub, raising=True)


# A structured-shaped draft whose only normative claim carries NO citation ⇒ verify grounds nothing ⇒ the
# strict retry fires, so BOTH generate .format sites (generate + generate_retry) are exercised in one call.
_STRUCTURED_UNCITED = (
    "## Vaše situace\nChcete přijmout prvního zaměstnance.\n"
    "## Co se na vás vztahuje\nMusíte se zaregistrovat okamžitě.\n"
    "## Co korpus nepokrývá\nPraktické kroky.\n"
)


async def test_answer_structured_on_does_not_keyerror_at_generate(monkeypatch):
    # THE regression: with structured_answer_enabled ON, answer() must complete (previously KeyError:'profile'
    # at the first generate .format). The uncited draft forces the strict retry so both .format sites run.
    prompts, gen = [], {}
    _structured_harness(monkeypatch, draft=_STRUCTURED_UNCITED, prompts=prompts, gen_calls=gen)
    res = await ans.answer("Beru prvního zaměstnance, co musím udělat?", method="cte")
    assert res is not None  # no exception raised = the KeyError is fixed
    assert gen.get("generate") == 1 and gen.get("generate_retry") == 1  # both .format sites exercised
    # the STRUCTURED templates (with the PROFILE + PRACTICAL FACTS blocks) were what actually got formatted
    assert prompts and all("PROFILE:" in p and "PRACTICAL FACTS:" in p for p in prompts)
    assert all("Vaše situace" in p for p in prompts)
    # the B1a verbatim deadline excerpt from the retrieved provision reached the PRACTICAL FACTS block
    assert any("do 8 dnů" in p for p in prompts)


async def test_answer_structured_on_threads_the_profile_context_into_the_prompt(monkeypatch):
    # profile-context supplied (intake OFF) ⇒ the whitelisted known facts render into the PROFILE block.
    prompts = []
    _structured_harness(monkeypatch, draft=_STRUCTURED_UNCITED, prompts=prompts)
    await ans.answer(
        "Beru prvního zaměstnance?", method="cte", project_context={"legal_form": "s.r.o.", "is_employer": True}
    )
    assert prompts
    assert any("legal_form: s.r.o." in p for p in prompts)
    assert any("is_employer: yes" in p for p in prompts)
