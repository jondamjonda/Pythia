"""WF-1 Increment 1 — the entity/role applicability gate (pure core + eval).

Covers the deterministic classifier, the binding matrix, and — most importantly — the two SAFETY INVARIANTS
that make `out_of_scope` fail-safe, checked EXHAUSTIVELY over the closed addressee × obligation-type domains:

  INV-1 (never-hide-when-unsure): an unknown/assumed project role, or an unparseable addressee, ALWAYS yields
         `binds == "yes"`. The filter cannot hide a finding when it does not know who the client is.
  INV-2 (out-of-scope needs a valid basis): a verdict is `out_of_scope` ONLY on a still-valid safe basis —
         legislature-addressed (transposition/discretion) OR a client-declared role exclusion with no top-up.

Plus the eval acceptance (`binding_recall == 1.0`, `out_of_scope_precision >= 0.95`) on the 79 gold findings.
Pure — no DB, no network, no LLM.
"""

from __future__ import annotations

from typing import get_args

import pytest

from regsuper.analysis import applicability_eval as ev
from regsuper.analysis.applicability import (
    AddresseeClass,
    ObligationType,
    addressee_class,
    decide_binding,
    obligation_type,
    project_entity_role,
)

_ADDRESSEES = list(get_args(AddresseeClass))
_OBLIGATION_TYPES = list(get_args(ObligationType))
_LEGISLATURE = {
    "transposition_instruction"
}  # ONLY pure "adopt laws" is out_of_scope-eligible (red-team §B)


# ── addressee normalization (EN + CZ free-text → closed class) ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "actor,expected",
    [
        ("AIFMs", "aifm"),
        ("AIFM", "aifm"),
        ("Investment firms", "mifid_investment_firm"),
        ("Investment firms and credit institutions", "mifid_investment_firm"),
        ("Member States", "member_state"),
        ("Legal or entrepreneurial natural person", "any_person"),
        ("depositary", "depositary"),
        ("external valuer", "external_valuer"),
        ("prime broker", "prime_broker"),
        ("investment company", "investment_company_licence"),
        (
            "administrator of an investment fund or foreign investment fund",
            "investment_company_licence",
        ),
        ("foreign entity with permission under § 481", "investment_company_licence"),
        ("ručitel", "prospectus_responsible"),
        ("osoba, která sestavila prospekt", "prospectus_responsible"),
        ("something we have never seen", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_addressee_class(actor, expected):
    assert addressee_class(actor) == expected


def test_obligation_type_heuristic():
    assert (
        obligation_type(
            "member_state", "shall", "Member States shall adopt the laws necessary to comply"
        )
        == "transposition_instruction"
    )
    assert (
        obligation_type(
            "member_state",
            "shall",
            "require that the AIFMs provide the competent authorities information",
        )
        == "member_state_requires_entity"
    )
    assert (
        obligation_type(
            "member_state", "shall", "ensure that no AIFMs manage AIFs unless authorised"
        )
        == "member_state_requires_entity"
    )
    assert (
        obligation_type("member_state", "may", "authorise an external AIFM to provide services")
        == "member_state_discretion"
    )
    assert obligation_type("any_person", "shall") == "generic_offence"


# ── WF-5: the statement-driven obligation-type classifier ──────────────────────────────────────────────────
def test_wf5_safety_gate_entity_adopt_is_not_transposition():
    """THE load-bearing WF-5 invariant: an ENTITY duty whose text contains 'adopt'/'ensure' must classify as a
    substantive_duty, NEVER transposition_instruction — otherwise the matrix could hide a genuine AIFM duty."""
    assert (
        obligation_type(
            "aifm", "shall", "shall adopt a remuneration policy and ensure it is applied"
        )
        == "substantive_duty"
    )
    assert (
        obligation_type(
            "mifid_investment_firm",
            "shall",
            "shall adopt and implement organisational arrangements",
        )
        == "substantive_duty"
    )
    # transposition is reachable ONLY for a member_state addressee
    assert (
        obligation_type("member_state", "shall", "Member States shall adopt laws")
        == "transposition_instruction"
    )


def test_wf5_word_boundary_markers():
    """Regex word boundaries: 'adoption'/'adopted policy' inside an entity duty must not trip the adopt-law rule."""
    # non-member_state never reaches transposition regardless of wording (the gate), so assert the member_state
    # edge: a member_state line that only mentions 'adoption' as a noun (no \\badopt\\b verb, no require marker).
    assert (
        obligation_type("aifm", "shall", "shall facilitate the adoption of internal standards")
        == "substantive_duty"
    )


def test_wf5_prospectus_liability_from_statement():
    """An ambiguous (unknown) CZ prospectus cross-ref is RECLASSIFIED to liability_allocation from the statement
    (ZPKT s.34 'odpovídat za správnost…'), improving the label — while the guard still keeps it (fail-safe)."""
    assert (
        obligation_type(
            "unknown", "shall", "odpovídat za správnost a úplnost údajů uvedených v prospektu"
        )
        == "liability_allocation"
    )
    assert (
        obligation_type("prospectus_responsible", "shall", "odpovídat za obsah prospektu")
        == "liability_allocation"
    )


def test_wf5_generic_offence_from_statement():
    assert (
        obligation_type("unknown", "must_not", "a person who commits an offence under this Act")
        == "generic_offence"
    )
    assert (
        obligation_type("any_person", "must_not", "offer investment contrary to s.272")
        == "generic_offence"
    )


def test_wf5_substantive_duty_not_downgraded_by_liability_words():
    """A named entity's own duty is NOT reclassified to liability_allocation even if it mentions 'responsible'."""
    assert (
        obligation_type(
            "aifm", "shall", "the AIFM is responsible for the accuracy of its disclosures"
        )
        == "substantive_duty"
    )


def test_wf5_gold_safety_invariant_transposition_is_member_state_only():
    """Over ALL 79 gold rows: transposition_instruction is assigned ONLY to a member_state addressee (the single
    out_of_scope-eligible type can never attach to an entity duty)."""
    import json
    from pathlib import Path

    gold = json.loads(
        (
            Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "applicability_gold.json"
        ).read_text(encoding="utf-8")
    )
    for f in gold["findings"]:
        p = f.get("payload") or {}
        addr = addressee_class(p.get("actor"))
        otype = obligation_type(addr, p.get("modal"), f.get("statement"))
        if otype == "transposition_instruction":
            assert addr == "member_state", (
                f"transposition on non-member_state addressee {addr!r} — could hide a duty"
            )


def test_eval_reports_transposition_safe():
    report = ev.run()
    assert report["no_exclude"]["transposition_safe"] is True
    assert report["pure_aifm"]["transposition_safe"] is True
    assert report["ac_pass"] is True
    assert obligation_type("investment_company_licence", "shall") == "licence_scope_rule"
    assert obligation_type("depositary", "shall") == "counterparty_duty"
    assert obligation_type("prospectus_responsible", "shall") == "liability_allocation"
    assert obligation_type("aifm", "shall") == "substantive_duty"
    assert obligation_type("unknown", None) == "unknown"


# ── project role derivation ────────────────────────────────────────────────────────────────────────────────
def test_project_entity_role_aifm_plain():
    r = project_entity_role("aifm", "EU AIFM marketing its own AIF")
    assert r["primary"] == "aifm" and r["excludes"] == [] and r["assumed"] is False
    assert r["has_top_up"] is True  # conservative default: MiFID may still reach via top-up


def test_project_entity_role_pure_aifm_excludes_and_no_topup():
    r = project_entity_role(
        "aifm", "pure AIFM, not a MiFID investment firm, no MiFID top-up services"
    )
    assert r["primary"] == "aifm"
    assert "mifid_investment_firm" in r["excludes"]
    assert r["has_top_up"] is False


def test_project_entity_role_unknown_is_assumed():
    r = project_entity_role(None)
    assert r["primary"] == "unknown" and r["assumed"] is True


# ── matrix spot-checks ─────────────────────────────────────────────────────────────────────────────────────
def test_matrix_aifm_substantive_binds():
    role = project_entity_role("aifm", "AIFM")
    v = decide_binding(role, "aifm", "substantive_duty")
    assert v["binds"] == "yes"


def test_matrix_member_state_adopt_laws_out_of_scope():
    role = project_entity_role("aifm", "AIFM")
    v = decide_binding(role, "member_state", "transposition_instruction")
    assert v["binds"] == "out_of_scope" and v["oos_basis"] == "legislature"


def test_matrix_member_state_requires_entity_is_conditional_not_hidden():
    """Red-team §B: 'Member States shall require that AIFMs …' binds the AIFM in substance — must NOT be hidden."""
    role = project_entity_role("aifm", "AIFM")
    v = decide_binding(role, "member_state", "member_state_requires_entity")
    assert v["binds"] == "conditional" and v["oos_basis"] is None


def test_matrix_member_state_discretion_is_conditional_not_hidden():
    role = project_entity_role("aifm", "AIFM")
    v = decide_binding(role, "member_state", "member_state_discretion")
    assert v["binds"] == "conditional" and v["oos_basis"] is None


def test_matrix_any_person_generic_offence_binds():
    role = project_entity_role("aifm", "AIFM")
    v = decide_binding(role, "any_person", "generic_offence")
    assert v["binds"] == "yes"


def test_matrix_mifid_conditional_without_declared_exclusion():
    role = project_entity_role("aifm", "AIFM")  # no exclusion, has_top_up True
    v = decide_binding(role, "mifid_investment_firm", "substantive_duty")
    assert v["binds"] == "conditional"


def test_matrix_mifid_out_of_scope_only_on_declared_exclusion():
    role = project_entity_role("aifm", "pure AIFM, no MiFID top-up")
    v = decide_binding(role, "mifid_investment_firm", "substantive_duty")
    assert v["binds"] == "out_of_scope" and v["oos_basis"] == "declared_exclusion"


def test_matrix_counterparty_is_conditional():
    role = project_entity_role("aifm", "AIFM")
    assert decide_binding(role, "depositary", "counterparty_duty")["binds"] == "conditional"


# ── INV-1: never hide a finding when the role/addressee is unknown ─────────────────────────────────────────
def test_inv1_unknown_project_keeps_everything():
    role = project_entity_role(None)  # assumed
    for a in _ADDRESSEES:
        for t in _OBLIGATION_TYPES:
            v = decide_binding(role, a, t)
            assert v["binds"] == "yes", f"unknown project must keep ({a},{t}) but got {v['binds']}"


def test_inv1_unknown_addressee_keeps_finding():
    role = project_entity_role("aifm", "AIFM")
    for t in _OBLIGATION_TYPES:
        v = decide_binding(role, "unknown", t)
        assert v["binds"] == "yes", f"unparseable addressee must keep, got {v['binds']} for {t}"


# ── INV-2: out_of_scope only ever rests on a valid safe basis ──────────────────────────────────────────────
@pytest.mark.parametrize("declared", [False, True])
def test_inv2_out_of_scope_requires_valid_basis(declared):
    intent = "pure AIFM, no MiFID top-up" if declared else "EU AIFM"
    role = project_entity_role("aifm", intent)
    for a in _ADDRESSEES:
        for t in _OBLIGATION_TYPES:
            v = decide_binding(role, a, t)
            if v["binds"] == "out_of_scope":
                legislature_ok = t in _LEGISLATURE
                exclusion_ok = declared and a in set(role["excludes"]) and not role["has_top_up"]
                assert legislature_ok or exclusion_ok, (
                    f"out_of_scope with NO valid basis: addressee={a}, type={t}, declared={declared}"
                )


def test_inv2_demotes_to_conditional_not_dropped():
    """A would-be out_of_scope with no valid basis is demoted to conditional (never dropped/hidden)."""
    role = project_entity_role("aifm", "AIFM")  # no declared exclusion
    # mifid substantive WANTS exclusion-basis but none declared → conditional, never out_of_scope.
    v = decide_binding(role, "mifid_investment_firm", "substantive_duty")
    assert v["binds"] == "conditional" and v["oos_basis"] is None


def test_confidence_floor_demotes_out_of_scope():
    role = project_entity_role("aifm", "pure AIFM, no MiFID top-up")
    high = decide_binding(
        role, "mifid_investment_firm", "substantive_duty", binds_confidence=0.9, floor=0.5
    )
    low = decide_binding(
        role, "mifid_investment_firm", "substantive_duty", binds_confidence=0.1, floor=0.5
    )
    assert high["binds"] == "out_of_scope"
    assert low["binds"] == "conditional" and low["guarded"] is True


def test_confidence_floor_zero_is_inert():
    """floor=0.0 must never demote, even at the lowest possible confidence (documented inert)."""
    role = project_entity_role("aifm", "pure AIFM, no MiFID top-up")
    v = decide_binding(
        role, "mifid_investment_firm", "substantive_duty", binds_confidence=0.0, floor=0.0
    )
    assert v["binds"] == "out_of_scope" and v["guarded"] is False


def test_confidence_floor_only_applies_to_out_of_scope():
    """A high floor must NOT touch a 'yes' or 'conditional' verdict — the floor gates only out_of_scope."""
    role = project_entity_role("aifm", "AIFM")
    yes = decide_binding(role, "aifm", "substantive_duty", binds_confidence=0.01, floor=0.99)
    cond = decide_binding(
        role, "depositary", "counterparty_duty", binds_confidence=0.01, floor=0.99
    )
    assert yes["binds"] == "yes"
    assert cond["binds"] == "conditional"


def test_has_top_up_none_is_conservative_keeps_mifid():
    """SF-1: has_top_up=None is AMBIGUOUS, not a 'no top-up' declaration. A hand-built role with None must
    NOT enable the MiFID out_of_scope basis — the duty stays conditional (kept)."""
    role = {
        "primary": "aifm",
        "excludes": ["mifid_investment_firm"],
        "has_top_up": None,
        "assumed": False,
    }
    v = decide_binding(role, "mifid_investment_firm", "substantive_duty")
    assert v["binds"] == "conditional", (
        "None has_top_up must be treated as 'might have top-up' (fail-safe)"
    )
    # contrast: an EXPLICIT False does enable the declared-exclusion out_of_scope
    role_false = {**role, "has_top_up": False}
    assert (
        decide_binding(role_false, "mifid_investment_firm", "substantive_duty")["binds"]
        == "out_of_scope"
    )


def test_excludes_none_equals_empty_list():
    """SF-5: excludes=None and excludes=[] must produce identical verdicts across the whole domain."""
    base = {"primary": "aifm", "assumed": False, "has_top_up": True}
    role_none = {**base, "excludes": None}
    role_empty = {**base, "excludes": []}
    for a in _ADDRESSEES:
        for t in _OBLIGATION_TYPES:
            assert (
                decide_binding(role_none, a, t)["binds"]
                == decide_binding(role_empty, a, t)["binds"]
            )


@pytest.mark.parametrize("top_up_state", [True, False, None])
def test_inv2_holds_under_all_top_up_states(top_up_state):
    """Extend the INV-2 sweep with has_top_up as a third axis (incl. the None case from SF-1/SF-2)."""
    role = {
        "primary": "aifm",
        "excludes": ["mifid_investment_firm"],
        "has_top_up": top_up_state,
        "assumed": False,
    }
    eff_top_up = True if top_up_state is None else top_up_state
    for a in _ADDRESSEES:
        for t in _OBLIGATION_TYPES:
            v = decide_binding(role, a, t)
            if v["binds"] == "out_of_scope":
                legislature_ok = t in _LEGISLATURE
                exclusion_ok = a in set(role["excludes"]) and not eff_top_up
                assert legislature_ok or exclusion_ok, (
                    f"out_of_scope with NO valid basis: addressee={a}, type={t}, top_up={top_up_state}"
                )


def test_pf1_czech_prospectus_crossref_not_classed_as_generic_offence():
    """PF-1: the bare 'osoba' rule was dropped — CZ prospectus cross-refs must NOT become any_person."""
    assert addressee_class("osoba uvedená v odstavci 1") == "unknown"
    assert addressee_class("osoby uvedené v odstavci 1") == "unknown"
    # the genuine generic-offence addressee (English) is unaffected
    assert addressee_class("Legal or entrepreneurial natural person") == "any_person"


# ── the eval against the 79 gold findings ──────────────────────────────────────────────────────────────────
def test_eval_runs_and_passes_acceptance():
    report = ev.run()
    assert report["n"] == 79
    for cfg in ("no_exclude", "pure_aifm"):
        c = report[cfg]
        assert c["binding_recall"] == 1.0, f"{cfg}: a binding duty was hidden (binding_recall<1.0)"
        assert c["out_of_scope_precision"] >= 0.95, f"{cfg}: out_of_scope precision too low"
    assert report["ac_pass"] is True


def test_eval_pure_aifm_reduces_more_scope_than_no_exclude():
    """A stronger client declaration must set aside strictly more (the MiFID firm-conduct duties)."""
    report = ev.run()
    assert report["pure_aifm"]["n_out_of_scope"] > report["no_exclude"]["n_out_of_scope"]
