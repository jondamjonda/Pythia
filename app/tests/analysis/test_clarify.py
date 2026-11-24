"""Track A / A3 STAGE-1 — unit tests for the pure deterministic clarify module.

Pins the matter-typing lexicon, the slot resolution precedence (typed slots → profile →
deterministic query read), the ≤3-question selection, and the structural slot validator.
No DB, no LLM, no I/O — the module is pure.
"""

from __future__ import annotations

from types import SimpleNamespace

from regsuper.analysis import clarify


def _ctx(**kw):
    """A duck-typed AnswerContext-ish stub (getattr-based; the module only getattrs)."""
    base = dict(
        legal_form=None,
        headcount_band=None,
        municipality_code=None,
        is_employer=False,
        processes_personal_data=False,
        cross_border=False,
        handles_client_money=False,
        has_real_estate=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# Matter typing (deterministic keyword lexicon, NO LLM)
# ---------------------------------------------------------------------------
def test_detect_matter_employment() -> None:
    assert clarify.detect_matter("Musím dát zaměstnanci výpověď?") == clarify.MATTER_EMPLOYMENT


def test_detect_matter_vat() -> None:
    assert clarify.detect_matter("Kdy se stanu plátcem DPH?") == clarify.MATTER_VAT


def test_detect_matter_licensing() -> None:
    assert clarify.detect_matter("Potřebuji živnostenské oprávnění?") == clarify.MATTER_LICENSING


def test_detect_matter_data_protection() -> None:
    assert (
        clarify.detect_matter("Jak splnit GDPR u osobních údajů?") == clarify.MATTER_DATA_PROTECTION
    )


def test_detect_matter_general_fallback() -> None:
    # no lexicon hit → general (which has NO required slots ⇒ never clarifies)
    assert clarify.detect_matter("Jaké je počasí?") == clarify.MATTER_GENERAL
    assert clarify.MATTER_REQUIRED_SLOTS[clarify.MATTER_GENERAL] == ()


# ---------------------------------------------------------------------------
# Slot resolution — precedence (typed slots → profile → query read)
# ---------------------------------------------------------------------------
def test_general_matter_never_needs_clarify() -> None:
    res = clarify.resolve_slots("Jaké je počasí?", answer_context=None)
    assert res.matter_type == clarify.MATTER_GENERAL
    assert res.needs_clarify is False
    assert res.missing_required == []


def test_missing_required_when_no_profile_and_no_query_signal() -> None:
    # VAT requires legal_form + cross_border; a bare query pins neither → both missing.
    res = clarify.resolve_slots("Kdy se stanu plátcem DPH?", answer_context=None)
    assert res.matter_type == clarify.MATTER_VAT
    assert res.missing_required == [clarify.SLOT_LEGAL_FORM, clarify.SLOT_CROSS_BORDER]


def test_profile_resolves_legal_form() -> None:
    res = clarify.resolve_slots(
        "Kdy se stanu plátcem DPH?", answer_context=_ctx(legal_form="sro", cross_border=True)
    )
    # legal_form from profile, cross_border=True gate from profile → nothing missing
    assert res.resolved[clarify.SLOT_LEGAL_FORM] == "sro"
    assert res.resolved[clarify.SLOT_CROSS_BORDER] == "yes"
    assert res.needs_clarify is False


def test_query_regex_resolves_legal_form() -> None:
    # query mentions s.r.o. → legal_form resolved deterministically without a profile
    res = clarify.resolve_slots(
        "Naše s.r.o. — kdy se stane plátcem DPH při obchodu do zahraničí?",
        answer_context=None,
    )
    assert res.resolved[clarify.SLOT_LEGAL_FORM] == "sro"
    # "do zahraničí" affirms cross_border from the query
    assert res.resolved[clarify.SLOT_CROSS_BORDER] == "yes"
    assert res.needs_clarify is False


def test_typed_slots_take_precedence() -> None:
    # even with a profile legal_form, an explicit resubmitted slot wins
    res = clarify.resolve_slots(
        "Kdy se stanu plátcem DPH?",
        answer_context=_ctx(legal_form="osvc", cross_border=True),
        slots_provided={"legal_form": "as"},
    )
    assert res.resolved[clarify.SLOT_LEGAL_FORM] == "as"


def test_profile_gate_false_counts_as_resolved() -> None:
    # data_protection requires processes_personal_data + cross_border; a profile that set
    # both gates to False is an explicit answer → resolved 'no', not missing.
    res = clarify.resolve_slots(
        "Jak splnit GDPR u osobních údajů?",
        answer_context=_ctx(processes_personal_data=False, cross_border=False),
    )
    assert res.resolved[clarify.SLOT_PROCESSES_PERSONAL_DATA] == "no"
    assert res.resolved[clarify.SLOT_CROSS_BORDER] == "no"
    assert res.needs_clarify is False


def test_region_resolved_from_municipality_code() -> None:
    # licensing requires legal_form + region; region comes from the profile obec code (AQ-05)
    res = clarify.resolve_slots(
        "Potřebuji živnostenské oprávnění?",
        answer_context=_ctx(legal_form="sro", municipality_code="554782"),
    )
    assert res.resolved[clarify.SLOT_REGION] == "554782"
    assert res.needs_clarify is False


def test_no_context_leaves_gates_unresolved() -> None:
    # with NO answer_context, gate booleans are NOT inferred False from silence → missing
    res = clarify.resolve_slots("Jak splnit GDPR u osobních údajů?", answer_context=None)
    assert clarify.SLOT_PROCESSES_PERSONAL_DATA in res.missing_required


# ---------------------------------------------------------------------------
# Question selection — ≤3 fixed questions
# ---------------------------------------------------------------------------
def test_clarify_questions_capped_at_three() -> None:
    # employment requires 3 slots; all missing → exactly 3 questions
    res = clarify.resolve_slots("Musím dát zaměstnanci výpověď?", answer_context=None)
    qs = clarify.clarify_questions(res.missing_required, lang="cs")
    assert len(qs) <= clarify.MAX_CLARIFY_QUESTIONS
    assert len(qs) == 3
    assert all(q.text for q in qs)
    assert [q.slot for q in qs] == res.missing_required


def test_clarify_questions_lang_fallback() -> None:
    qs = clarify.clarify_questions([clarify.SLOT_LEGAL_FORM], lang="de")  # unsupported → cs
    assert qs[0].text == clarify.CLARIFY_CATALOG[clarify.SLOT_LEGAL_FORM]["cs"]
    qs_en = clarify.clarify_questions([clarify.SLOT_LEGAL_FORM], lang="en")
    assert qs_en[0].text == clarify.CLARIFY_CATALOG[clarify.SLOT_LEGAL_FORM]["en"]


# ---------------------------------------------------------------------------
# Slot validation (structural)
# ---------------------------------------------------------------------------
def test_validate_slots_ok() -> None:
    assert clarify.validate_slots({"legal_form": "sro", "is_employer": "yes"}) is None


def test_validate_slots_unknown_key() -> None:
    assert clarify.validate_slots({"bogus": "x"}) is not None


def test_validate_slots_bad_value() -> None:
    assert clarify.validate_slots({"legal_form": "not-a-form"}) is not None


def test_validate_slots_region_free_string() -> None:
    assert clarify.validate_slots({"region": "Praha"}) is None
    assert clarify.validate_slots({"region": ""}) is not None
