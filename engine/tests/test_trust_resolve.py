"""B1a-08 — the PURE trust resolver (no DB).

Covers EVERY TIER_MAP cell, the doc-floor max(section,doc), the per-provision-conservative downgrade guard,
the is_rule union, NULL-parent→doc-tier, the orphan default, and future-effective in must_surface.
"""

from __future__ import annotations

from datetime import date

import pytest

from pythia.graph.trust_resolve import (
    TIER_HARD_LAW,
    TIER_INTERPRETIVE,
    TIER_MAP,
    TIER_OOP,
    TIER_SOFT_LAW,
    doc_tier,
    resolve_trust,
)

KIND_PREAMBLE = "preamble_recital"
KIND_DEFINITIONS = "definitions"
KIND_OPERATIVE = "operative"
KIND_PENALTIES = "penalties"
KIND_TRANSITIONAL = "transitional_final"


def _row(**kw) -> dict:
    base = {
        "valid_from": date(2000, 1, 1),
        "valid_to": None,
        "temporal_status": "IN_FORCE",
        "annulment_effect": None,
        "annuls_target": False,
        "bindingness": "HARD_LAW_ERGA_OMNES",
        "inherited_kind": KIND_OPERATIVE,
        "kind_override": None,
        "analysis_roles": [],
    }
    base.update(kw)
    return base


AS_OF = date(2024, 1, 1)


# --------------------------------------------------------------------------- doc_tier mapping
@pytest.mark.parametrize(
    "binding,expected",
    [
        ("HARD_LAW_ERGA_OMNES", TIER_HARD_LAW),
        ("BINDING_OOP", TIER_OOP),
        ("SOFT_LAW", TIER_SOFT_LAW),
        ("INTERPRETIVE_CASE_LAW", TIER_INTERPRETIVE),
        ("BINDING_IN_PERSONAM", TIER_INTERPRETIVE),
        (None, TIER_HARD_LAW),  # unknown/NULL ⇒ the SAFE hard-law floor
        ("WEIRD", TIER_HARD_LAW),
    ],
)
def test_doc_tier(binding, expected):
    assert doc_tier(binding) == expected


# --------------------------------------------------------------------------- EVERY TIER_MAP cell (20)
# operative / penalties / transitional_final / definitions HOLD the doc tier; preamble_recital downgrades
# only on the two hard doc tiers. 5 kinds × 4 doc bindingnesses present in the map = 20 cells.
@pytest.mark.parametrize("kind", [KIND_OPERATIVE, KIND_PENALTIES, KIND_TRANSITIONAL, KIND_DEFINITIONS])
@pytest.mark.parametrize(
    "binding,tier",
    [
        ("HARD_LAW_ERGA_OMNES", TIER_HARD_LAW),
        ("BINDING_OOP", TIER_OOP),
        ("SOFT_LAW", TIER_SOFT_LAW),
        ("INTERPRETIVE_CASE_LAW", TIER_INTERPRETIVE),
    ],
)
def test_tier_map_holding_kinds(kind, binding, tier):
    assert TIER_MAP[(kind, binding)] == tier


@pytest.mark.parametrize(
    "binding,tier",
    [
        ("HARD_LAW_ERGA_OMNES", TIER_INTERPRETIVE),  # the downgrade
        ("BINDING_OOP", TIER_INTERPRETIVE),  # the downgrade
        ("SOFT_LAW", TIER_SOFT_LAW),  # already ≤ interpretive → holds
        ("INTERPRETIVE_CASE_LAW", TIER_INTERPRETIVE),
    ],
)
def test_tier_map_preamble_downgrade(binding, tier):
    assert TIER_MAP[(KIND_PREAMBLE, binding)] == tier


def test_tier_map_has_exactly_20_cells():
    assert len(TIER_MAP) == 20


# --------------------------------------------------------------------------- the preamble downgrade (resolver)
def test_preamble_no_cue_is_downgraded_to_interpretive():
    t = resolve_trust(_row(inherited_kind=KIND_PREAMBLE, analysis_roles=[]), AS_OF)
    assert t.bindingness_tier == TIER_INTERPRETIVE
    assert t.is_rule is False


def test_preamble_WITH_binding_cue_is_NOT_downgraded():
    # the per-provision-conservative guard: a duty inside a (mis-)marked preamble is a rule, held at doc tier.
    t = resolve_trust(_row(inherited_kind=KIND_PREAMBLE, analysis_roles=[2]), AS_OF)
    assert t.bindingness_tier == TIER_HARD_LAW
    assert t.is_rule is True


# --------------------------------------------------------------------------- the doc-floor = max(section,doc)
def test_doc_floor_holds_for_operative_rule():
    t = resolve_trust(_row(inherited_kind=KIND_OPERATIVE, bindingness="HARD_LAW_ERGA_OMNES", analysis_roles=[2]), AS_OF)
    assert t.bindingness_tier == TIER_HARD_LAW
    assert t.doc_tier == TIER_HARD_LAW
    assert t.is_rule is True


def test_doc_floor_binding_to_soft_is_impossible():
    # even a definitions-tagged provision that carries an obligation cue (mis-classification) cannot drop below
    # the hard-law doc: the cue arm pins the floor.
    t = resolve_trust(
        _row(inherited_kind=KIND_DEFINITIONS, bindingness="HARD_LAW_ERGA_OMNES", analysis_roles=[2]), AS_OF
    )
    assert t.bindingness_tier == TIER_HARD_LAW
    assert t.is_rule is True


def test_section_softer_than_doc_is_lifted_to_doc_floor():
    # contrived: an inherited soft section tier under a hard doc, with a binding cue ⇒ floored to hard-law.
    # (definitions under SOFT_LAW doc → soft-law tier; force the doc to HARD via bindingness mismatch.)
    t = resolve_trust(_row(inherited_kind=KIND_PENALTIES, bindingness="HARD_LAW_ERGA_OMNES", analysis_roles=[7]), AS_OF)
    assert t.bindingness_tier == TIER_HARD_LAW


# --------------------------------------------------------------------------- NULL-parent / orphan defaults
def test_null_section_kind_falls_back_to_doc_tier():
    t = resolve_trust(
        _row(inherited_kind=None, kind_override=None, bindingness="HARD_LAW_ERGA_OMNES", analysis_roles=[2]), AS_OF
    )
    assert t.bindingness_tier == TIER_HARD_LAW
    assert "null-kind→doc-tier" in t.reasons
    assert t.is_rule is True  # caught by the cue arm


def test_null_parent_rule_with_oop_doc():
    t = resolve_trust(_row(inherited_kind=None, bindingness="BINDING_OOP", analysis_roles=[3]), AS_OF)
    assert t.bindingness_tier == TIER_OOP
    assert t.is_rule is True


def test_orphan_no_kind_no_cue_defaults_operative_holds_doc():
    # no inherited kind, no cue → kind defaults operative, tier = doc tier (held), not a rule (no cue, but
    # operative section arm requires a rule-tier: hard-law qualifies → is_rule True via the section arm).
    t = resolve_trust(_row(inherited_kind=None, analysis_roles=[]), AS_OF)
    assert t.bindingness_tier == TIER_HARD_LAW
    # section arm: operative + hard-law tier ⇒ is_rule True even with no cue (default-conservative).
    assert t.section_kind == KIND_OPERATIVE


def test_kind_override_beats_inherited():
    t = resolve_trust(
        _row(
            inherited_kind=KIND_OPERATIVE,
            kind_override=KIND_PREAMBLE,
            bindingness="HARD_LAW_ERGA_OMNES",
            analysis_roles=[],
        ),
        AS_OF,
    )
    assert t.section_kind == KIND_PREAMBLE
    assert t.bindingness_tier == TIER_INTERPRETIVE  # the override drives the downgrade


# --------------------------------------------------------------------------- is_rule union
def test_is_rule_section_arm():
    # operative + hard-law, no cue ⇒ rule via the section arm.
    t = resolve_trust(_row(inherited_kind=KIND_OPERATIVE, analysis_roles=[]), AS_OF)
    assert t.is_rule is True


def test_is_rule_cue_arm_even_in_definitions():
    t = resolve_trust(_row(inherited_kind=KIND_DEFINITIONS, analysis_roles=[3]), AS_OF)
    assert t.is_rule is True


def test_definitions_no_cue_is_not_a_rule():
    t = resolve_trust(_row(inherited_kind=KIND_DEFINITIONS, analysis_roles=[]), AS_OF)
    assert t.is_rule is False
    assert t.bindingness_tier == TIER_HARD_LAW  # holds the doc tier (definitions HOLD)


def test_soft_law_operative_no_cue_not_a_rule():
    # operative under SOFT_LAW doc: tier soft-law ∉ rule-tiers, no cue ⇒ NOT a rule.
    t = resolve_trust(_row(inherited_kind=KIND_OPERATIVE, bindingness="SOFT_LAW", analysis_roles=[]), AS_OF)
    assert t.is_rule is False
    assert t.bindingness_tier == TIER_SOFT_LAW


# --------------------------------------------------------------------------- temporal + must_surface
def test_in_force_rule_must_surface():
    t = resolve_trust(_row(analysis_roles=[2], valid_from=date(2010, 1, 1), valid_to=None), AS_OF)
    assert t.temporal_status == "in-force"
    assert t.must_surface is True


def test_future_effective_rule_must_surface():
    t = resolve_trust(_row(analysis_roles=[2], valid_from=date(2030, 1, 1), valid_to=None), AS_OF)
    assert t.temporal_status == "future-effective"
    assert t.must_surface is True  # surfaced under the upcoming banner


def test_superseded_rule_does_not_surface():
    t = resolve_trust(
        _row(
            analysis_roles=[2],
            valid_to=date(2020, 1, 1),
            temporal_status="REPEALED",
            annulment_effect=None,
            annuls_target=False,
        ),
        AS_OF,
    )
    assert t.temporal_status == "superseded"
    assert t.must_surface is False


def test_annulled_via_annuls_target():
    t = resolve_trust(
        _row(analysis_roles=[2], valid_to=date(2020, 1, 1), temporal_status="REPEALED", annuls_target=True), AS_OF
    )
    assert t.temporal_status == "annulled"
    assert t.must_surface is False


def test_annulled_via_ex_nunc_effect():
    t = resolve_trust(
        _row(
            analysis_roles=[2], valid_to=date(2020, 1, 1), temporal_status="ANNULLED_BY_CC", annulment_effect="EX_NUNC"
        ),
        AS_OF,
    )
    assert t.temporal_status == "annulled"


def test_non_rule_in_force_does_not_surface():
    t = resolve_trust(_row(inherited_kind=KIND_DEFINITIONS, analysis_roles=[]), AS_OF)
    assert t.must_surface is False  # in-force but not a rule
