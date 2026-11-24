"""Track A / T6' — the INTAKE VERIFIER REGISTRY (intake mirror of the Verification Loop) + bounded repair.

Deterministic tests (LLM-free via stubs, DB-free — the registry is pure). Covers:
  • IV-SPAN — an ungrounded fact (evidence_quote not a verbatim substring of the query) is DROPPED, a
    verbatim (whitespace-folded) quote is kept, and the offset is RECOMPUTED on the verdict.
  • IV-ENUM — an out-of-vocab field is DROPPED (parity with intake._FACT_FIELDS).
  • IV-CONFLICT — a profile fact WINS over a conflicting query fact on the same field + the conflict is
    RECORDED (never silent); a same-value profile field drops the query fact silently.
  • IV-TEMPORAL — an extracted date is display-only (suggested_as_of recorded, request as_of never mutated).
  • validate is IDEMPOTENT — re-validating an already-valid proposal yields an equal fact/sub set.
  • zero-valid-subs ⇒ ValueError (the caller's legacy-fallback trigger).
  • run_intake bounded REPAIR — fires on >50% facts dropped, is MONOTONE (a twice-failing fact is dropped,
    never re-repaired), and zero-valid-subs-after-repair ⇒ the EXACT legacy fallback.
  • an ALL-VALID proposal yields the SAME facts/subs the committed A2 inline validate_intake produced.
"""

from __future__ import annotations

import pytest

from pythia.reasoning.intake import (
    IntakeFact,
    legacy_record,
    run_intake,
    validate_intake,
)
from pythia.reasoning.intake_verify import (
    _ENUM_VOCAB,
    iv_span,
    iv_temporal,
    validate,
)

_Q = "Beru prvního zaměstnance do své malé restaurace, co musím udělat?"


# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
# Stub LLM — returns a scripted sequence of json() payloads (one per call) so a repair second-call is
# deterministic; records the prompts + stages it was asked.
# ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _SeqLLM:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls: list[tuple[str, str | None]] = []

    async def json(self, prompt, schema, *, tier="workhorse", stage=None):
        self.calls.append((prompt, stage))
        return self._payloads[min(len(self.calls) - 1, len(self._payloads) - 1)]

    async def text(self, prompt, *, system=None, tier="workhorse", stage=None):
        return ""

    async def available(self):
        return True


# ═════════ IV-SPAN ═════════
def test_iv_span_drops_ungrounded_fact():
    raw = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [
            {"field": "sector", "value": "gastro", "evidence_quote": "restaurace"},  # grounded
            {"field": "legal_form", "value": "s.r.o.", "evidence_quote": "společnost s ručením omezeným"},
        ],  # ungrounded
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    rec = validate(_Q, raw)
    assert [f.field for f in rec.facts] == ["sector"]
    # the ungrounded fact produced an IV-SPAN drop verdict
    span_drops = [v for v in rec.iv_verdicts if v["verifier"] == "IV-SPAN" and v["status"] == "drop"]
    assert any(v["reason"] == "quote_not_in_query" for v in span_drops)


def test_iv_span_recomputes_offset_not_trusting_model():
    folded_q = "beru prvního zaměstnance do své malé restaurace, co musím udělat?"
    grounded, verdict = iv_span("fact:0", "restaurace", folded_q)
    assert grounded is True
    assert verdict.evidence["offset"] == folded_q.find("restaurace")  # RECOMPUTED, matches str.find
    assert verdict.evidence["length"] == len("restaurace")


# ═════════ IV-ENUM ═════════
def test_iv_enum_drops_out_of_vocab_field():
    raw = {
        "sub_questions": ["Q1 statute phrasing", "Q2 statute phrasing"],
        "facts": [{"field": "cascade_verdict", "value": "x", "evidence_quote": "restaurace"}],
        "matter_type": "x",
        "jurisdiction": ["CZ"],
    }
    rec = validate(_Q, raw)
    assert rec.facts == []
    assert any(v["verifier"] == "IV-ENUM" and v["reason"] == "field_out_of_vocab" for v in rec.iv_verdicts)


def test_iv_enum_vocab_parity_with_intake():
    # the registry's vocab is IDENTICAL to intake's closed enum (the parity guard)
    from pythia.reasoning.intake import _FACT_FIELDS

    assert _ENUM_VOCAB == _FACT_FIELDS


# ═════════ IV-CONFLICT ═════════
def test_iv_conflict_profile_wins_and_records():
    q = "Jsem o.s.v.č. a beru prvního zaměstnance, co musím udělat?"
    raw = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [{"field": "legal_form", "value": "o.s.v.č.", "evidence_quote": "o.s.v.č."}],
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    profile = [
        IntakeFact(field="legal_form", value="s.r.o.", evidence_quote="profile:business_profile", source="profile")
    ]
    rec = validate(q, raw, profile)
    lf = [f for f in rec.facts if f.field == "legal_form"]
    assert len(lf) == 1 and lf[0].value == "s.r.o." and lf[0].source == "profile"  # profile WINS
    assert rec.profile_conflicts == [{"field": "legal_form", "profile_value": "s.r.o.", "query_value": "o.s.v.č."}]
    assert any(v["verifier"] == "IV-CONFLICT" and v["status"] == "conflict" for v in rec.iv_verdicts)


def test_iv_conflict_same_value_no_conflict_recorded():
    q = "Jsem s.r.o. a beru prvního zaměstnance, co musím udělat?"
    raw = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [{"field": "legal_form", "value": "S.R.O.", "evidence_quote": "s.r.o."}],
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    profile = [
        IntakeFact(field="legal_form", value="s.r.o.", evidence_quote="profile:business_profile", source="profile")
    ]
    rec = validate(q, raw, profile)
    assert rec.profile_conflicts == []  # same value ⇒ no conflict
    lf = [f for f in rec.facts if f.field == "legal_form"]
    assert len(lf) == 1 and lf[0].source == "profile"  # profile fact wins silently


# ═════════ IV-TEMPORAL ═════════
def test_iv_temporal_is_display_only():
    v = iv_temporal("fact:0", "temporal", "od roku 2024", "2024")
    assert v is not None
    assert v.status == "advisory" and v.evidence["display_only"] is True
    assert v.evidence["suggested_as_of"] == "2024"


def test_iv_temporal_none_when_no_date():
    assert iv_temporal("fact:0", "sector", "gastro", "restaurace") is None


# ═════════ idempotence + parity with A2 inline validator ═════════
def test_validate_is_idempotent():
    raw = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [{"field": "sector", "value": "gastro", "evidence_quote": "restaurace"}],
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    r1 = validate(_Q, raw)
    # feed r1 back as a raw proposal (its facts/subs are already valid) → equal fact/sub set
    raw2 = {
        "sub_questions": list(r1.sub_questions),
        "facts": [{"field": f.field, "value": f.value, "evidence_quote": f.evidence_quote} for f in r1.facts],
        "matter_type": r1.matter_type,
        "jurisdiction": r1.jurisdiction,
        "sufficiency": r1.sufficiency,
    }
    r2 = validate(_Q, raw2)
    assert [(f.field, f.value) for f in r1.facts] == [(f.field, f.value) for f in r2.facts]
    assert r1.sub_questions == r2.sub_questions


def test_all_valid_proposal_matches_a2_inline_validator():
    raw = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [{"field": "sector", "value": "gastro", "evidence_quote": "restaurace"}],
        "hyde_drafts": ["Zaměstnavatel je povinen..."],
        "sufficiency": "thin",
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    reg = validate(_Q, raw)
    inline = validate_intake(raw, _Q)  # the committed A2 inline validator (no registry)
    assert reg.sub_questions == inline.sub_questions
    assert [(f.field, f.value, f.evidence_quote) for f in reg.facts] == [
        (f.field, f.value, f.evidence_quote) for f in inline.facts
    ]
    assert reg.matter_type == inline.matter_type
    assert reg.jurisdiction == inline.jurisdiction
    assert reg.hyde_drafts == inline.hyde_drafts
    assert reg.sufficiency == inline.sufficiency


def test_validate_raises_below_two_subquestions():
    with pytest.raises(ValueError):
        validate(_Q, {"sub_questions": ["only one"], "matter_type": "x", "jurisdiction": ["CZ"]})


# ═════════ bounded repair (run_intake) ═════════
async def test_repair_fires_on_over_half_facts_dropped_and_is_monotone():
    # First pass: 3 facts proposed, 2 ungrounded (>50% dropped) ⇒ repair fires.
    first = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [
            {"field": "sector", "value": "gastro", "evidence_quote": "restaurace"},  # grounded
            {"field": "legal_form", "value": "s.r.o.", "evidence_quote": "NOT IN QUERY 1"},
            {"field": "tax", "value": "dph", "evidence_quote": "NOT IN QUERY 2"},
        ],
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    # Repair second pass: re-quotes ONE fact groundedly, but re-proposes an ungrounded one → it fails
    # the registry AGAIN and is DROPPED (monotone — never re-repaired).
    repair = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [
            {"field": "sector", "value": "gastro", "evidence_quote": "restaurace"},
            {
                "field": "employment",
                "value": "prvního zaměstnance",
                "evidence_quote": "prvního zaměstnance",
            },  # now grounded
            {"field": "legal_form", "value": "s.r.o.", "evidence_quote": "STILL NOT IN QUERY"},
        ],
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    llm = _SeqLLM([first, repair])
    rec = await run_intake(llm, _Q)
    assert rec.repaired is True
    assert len(llm.calls) == 2  # exactly ONE repair re-prompt
    assert llm.calls[1][1] == "intake_repair"  # tagged as the repair stage
    fields = sorted(f.field for f in rec.facts)
    assert fields == ["employment", "sector"]  # the twice-failing legal_form was DROPPED
    assert rec.fell_back is False


async def test_repair_zero_valid_subs_after_repair_falls_back_to_legacy():
    # First pass: only 1 valid sub ⇒ registry raises ⇒ repair fires. Repair ALSO yields <2 subs ⇒ legacy.
    first = {"sub_questions": ["only one"], "matter_type": "x", "jurisdiction": ["CZ"]}
    repair = {"sub_questions": ["still just one"], "matter_type": "x", "jurisdiction": ["CZ"]}
    llm = _SeqLLM([first, repair])
    rec = await run_intake(llm, _Q)
    assert rec == legacy_record(_Q)
    assert rec.fell_back is True and rec.sub_questions == [_Q]
    assert len(llm.calls) == 2  # one base + one (failed) repair


async def test_no_repair_when_first_pass_clean():
    clean = {
        "sub_questions": ["Povinnosti zaměstnavatele podle zákoníku práce", "Přihlášení zaměstnance k pojištění"],
        "facts": [{"field": "sector", "value": "gastro", "evidence_quote": "restaurace"}],
        "sufficiency": "thin",
        "matter_type": "employment",
        "jurisdiction": ["CZ"],
    }
    llm = _SeqLLM([clean])
    rec = await run_intake(llm, _Q)
    assert rec.repaired is False
    assert len(llm.calls) == 1  # no repair re-prompt
    assert "repaired" not in rec.as_trace()  # trace stays clean when no repair
    assert rec.iv_verdicts  # but the ledger IS present on the T6' path
