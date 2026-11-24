"""AQ-06 — AnswerContext shared schema (pure, DB-free).

Proves:
  1. context_hash is deterministic + construction-order independent (equal field
     values ⇒ equal hash; a changed field ⇒ a changed hash).
  2. from_business_profile maps the principle-9 fields, tolerates missing fields,
     and buckets turnover into a stable band.
"""

from __future__ import annotations

from types import SimpleNamespace

from regsuper.analysis.answer_context import AnswerContext


def test_context_hash_is_stable_and_deterministic():
    a = AnswerContext(
        legal_form="sro",
        nace_code="C25",
        derived_tags=["x", "y"],
        is_employer=True,
        preferred_language="cs",
    )
    b = AnswerContext(
        legal_form="sro",
        nace_code="C25",
        derived_tags=["x", "y"],
        is_employer=True,
        preferred_language="cs",
    )
    assert a.context_hash == b.context_hash
    # 64-hex sha256 digest.
    assert len(a.context_hash) == 64
    assert a.context_hash == a.context_hash  # idempotent


def test_context_hash_changes_when_a_field_changes():
    base = AnswerContext(legal_form="sro", is_employer=False)
    changed = AnswerContext(legal_form="sro", is_employer=True)
    assert base.context_hash != changed.context_hash


def test_context_hash_independent_of_kwarg_order():
    a = AnswerContext(legal_form="as", jurisdiction="CZ+EU", cross_border=True)
    b = AnswerContext(cross_border=True, jurisdiction="CZ+EU", legal_form="as")
    assert a.context_hash == b.context_hash


def test_from_business_profile_maps_fields():
    profile = SimpleNamespace(
        legal_form="sro",
        nace_code="C25",
        sector_override=None,
        additional_activities=[],
        headcount_band="10-49",
        annual_turnover_czk=5_000_000,
        processes_personal_data=True,
        handles_client_money=False,
        has_real_estate=True,
        cross_border=False,
        is_employer=True,
        regulated_activity=None,
        primary_jurisdiction="CZ",
        registered_municipality="554782",
        preferred_language="cs",
        derived_sector_tags=[],
        derived_activity_tags=[],
    )
    ctx = AnswerContext.from_business_profile(profile)
    assert ctx.legal_form == "sro"
    assert ctx.nace_code == "C25"
    assert ctx.headcount_band == "10-49"
    assert ctx.turnover_band == "2M-10M"  # 5M bucketed
    assert ctx.jurisdiction == "CZ"
    assert ctx.processes_personal_data is True
    assert ctx.has_real_estate is True
    assert ctx.is_employer is True
    assert ctx.cross_border is False
    assert ctx.municipality_code == "554782"
    assert ctx.preferred_language == "cs"
    assert isinstance(ctx.derived_tags, list)


def test_from_business_profile_tolerates_missing_fields():
    # A near-empty stub — every absent attribute must fall back to its default.
    ctx = AnswerContext.from_business_profile(SimpleNamespace())
    assert ctx.legal_form is None
    assert ctx.turnover_band is None  # no turnover ⇒ no band invented
    assert ctx.derived_tags == []
    assert ctx.processes_personal_data is False
    assert ctx.is_employer is False
    # Still hashable.
    assert len(ctx.context_hash) == 64


def test_turnover_bands():
    def band(v: int | None) -> str | None:
        return AnswerContext.from_business_profile(
            SimpleNamespace(annual_turnover_czk=v)
        ).turnover_band

    assert band(None) is None
    assert band(1_000_000) == "<2M"
    assert band(5_000_000) == "2M-10M"
    assert band(20_000_000) == "10M-50M"
    assert band(100_000_000) == "50M-250M"
    assert band(500_000_000) == "250M+"
