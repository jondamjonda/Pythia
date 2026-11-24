"""WF-2 Increment 1 — the EXTRACTION ⟂ APPLICABILITY confidence split (pure core + eval).

Covers the deterministic basis classifier, the label table, the keep-more floor, and — most importantly — the
SAFETY INVARIANTS that make the split fail-safe, checked EXHAUSTIVELY over the closed addressee × obligation-type
× binds domains and the 79 lawyer-confirmed gold findings:

  INV-A (orthogonality — THE acceptance): the applicability LABEL is a pure function of the legal basis. Varying
         ``extraction_confidence`` across the whole [0,1] range (incl. 0.0) and ``None`` leaves the label IDENTICAL.
         Extraction quality can NEVER lower (or raise) the legal-solidity rating.
  INV-B (generic offences are not extraction-fragile): every ``generic_offence``/``any_person`` verdict — incl.
         the 13 ZISIF s.614 offences at extraction conf ~0.899 — gets a HIGH (never ``low``) applicability label.
  INV-C (label-only / never-hide): this module changes NO ``binds`` verdict and drops nothing → ``binding_recall``
         stays 1.0. The floor is KEEP-MORE only (out_of_scope → conditional), never the reverse, and is barred
         from reading extraction confidence (the parameter does not exist).

Pure — no DB, no network, no LLM, no clock, no randomness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import get_args

import pytest

from regsuper.analysis import applicability_confidence as wf2
from regsuper.analysis.applicability import (
    AddresseeClass,
    Binds,
    ObligationType,
    addressee_class,
    decide_binding,
    obligation_type,
    project_entity_role,
)
from regsuper.analysis.applicability_confidence import (
    ApplicabilityConfidence,
    ConfidenceBasis,
    applicability_confidence_basis,
    applicability_confidence_floor,
)

_ADDRESSEES = list(get_args(AddresseeClass))
_OBLIGATION_TYPES = list(get_args(ObligationType))
_BINDS = list(get_args(Binds))
_LABELS = list(get_args(ApplicabilityConfidence))
_BASES = list(get_args(ConfidenceBasis))

_GOLD = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "applicability_gold.json"

# A representative spread of extraction-confidence values, including the gold s.614 value and both extremes.
_EXTRACTION_CONFS = [0.0, 0.1, 0.5, 0.8999999761581421, 0.9, 0.95, 1.0, None]


def _gold_findings() -> list[dict]:
    return json.loads(_GOLD.read_text(encoding="utf-8"))["findings"]


# ── shape / domain sanity ──────────────────────────────────────────────────────────────────────────────────
def test_basis_returns_well_formed_dict():
    out = applicability_confidence_basis("aifm", "substantive_duty", "yes")
    assert set(out.keys()) == {"basis", "label", "reason"}
    assert out["basis"] in _BASES
    assert out["label"] in _LABELS
    assert isinstance(out["reason"], str) and out["reason"]


def test_basis_label_is_always_in_domain_over_whole_grid():
    """Across the FULL addressee × type × binds grid every emitted basis/label is a member of the closed set."""
    for a in _ADDRESSEES:
        for t in _OBLIGATION_TYPES:
            for b in _BINDS:
                out = applicability_confidence_basis(a, t, b)
                assert out["basis"] in _BASES, f"unknown basis for ({a},{t},{b}): {out['basis']}"
                assert out["label"] in _LABELS, f"unknown label for ({a},{t},{b}): {out['label']}"


# ── the spec's named legal cases ───────────────────────────────────────────────────────────────────────────
def test_direct_aifm_substantive_is_addressee_match_high():
    """A direct AIFM substantive duty → addressee_match → HIGH (the most solid binding ground)."""
    out = applicability_confidence_basis("aifm", "substantive_duty", "yes")
    assert out["basis"] == "addressee_match"
    assert out["label"] == "high"


def test_generic_offence_binds_any_person_is_high():
    """A generic offence that binds any person → categorical → HIGH (NOT low, regardless of extraction conf)."""
    out = applicability_confidence_basis("any_person", "generic_offence", "yes")
    assert out["basis"] == "generic_offence_coverage"
    assert out["label"] == "high"


def test_transposition_vehicle_is_moderate():
    """member_state_requires_entity ('shall require that <entity> …') → transposition_vehicle → MODERATE."""
    out = applicability_confidence_basis(
        "member_state", "member_state_requires_entity", "conditional"
    )
    assert out["basis"] == "transposition_vehicle"
    assert out["label"] == "moderate"


def test_counterparty_duty_is_moderate():
    """A counterparty duty (depositary/valuer/prime broker) → counterparty_contractual → MODERATE."""
    out = applicability_confidence_basis("depositary", "counterparty_duty", "conditional")
    assert out["basis"] == "counterparty_contractual"
    assert out["label"] == "moderate"
    # the prime-broker / external-valuer counterparties classify the same way
    assert (
        applicability_confidence_basis("prime_broker", "counterparty_duty", "conditional")["label"]
        == "moderate"
    )
    assert (
        applicability_confidence_basis("external_valuer", "counterparty_duty", "conditional")[
            "label"
        ]
        == "moderate"
    )


def test_licence_scope_is_moderate():
    out = applicability_confidence_basis(
        "investment_company_licence", "licence_scope_rule", "conditional"
    )
    assert out["basis"] == "licence_scope_presumed"
    assert out["label"] == "moderate"


def test_member_state_discretion_is_conditional_assignment_moderate():
    out = applicability_confidence_basis("member_state", "member_state_discretion", "conditional")
    assert out["basis"] == "conditional_assignment"
    assert out["label"] == "moderate"


def test_out_of_scope_transposition_names_basis():
    """A legislature-addressed transposition set-aside is annotated (not hidden by this module)."""
    out = applicability_confidence_basis(
        "member_state", "transposition_instruction", "out_of_scope"
    )
    assert out["basis"] == "conditional_assignment"
    assert out["label"] in _LABELS


def test_out_of_scope_declared_exclusion_names_basis():
    out = applicability_confidence_basis(
        "mifid_investment_firm", "substantive_duty", "out_of_scope"
    )
    assert out["basis"] == "declared_exclusion"
    assert out["label"] == "high"  # the client ASSERTED the disqualifying fact → solid


def test_binding_yes_with_unknown_basis_is_floored_to_moderate_never_low():
    """CARDINAL-RULE FLOOR (review WF-2 CRITICAL): a DEFINITELY-binding duty (binds='yes') with an indeterminate
    basis — e.g. a substantive_duty whose addressee WF-1 could not parse but kept via the fail-safe guard — must
    NEVER be rated 'low' (that would understate a binding duty). The basis stays 'unknown' (honest)."""
    out = applicability_confidence_basis("unknown", "unknown", "yes")
    assert out["basis"] == "unknown" and out["label"] == "moderate"
    # the named failing input from the review: substantive_duty + unparseable addressee + kept-binding
    sd = applicability_confidence_basis("unknown", "substantive_duty", "yes")
    assert sd["label"] != "low"
    # 'low' stays reachable for a genuinely-uncertain CONDITIONAL (verify) verdict — honest, not understating
    cond = applicability_confidence_basis("unknown", "unknown", "conditional")
    assert cond["label"] == "low"


def test_no_binding_yes_verdict_is_ever_low_over_full_grid():
    """Sweep: across every addressee × obligation_type, a binds='yes' finding is never labelled 'low'."""
    for a in _ADDRESSEES:
        for t in _OBLIGATION_TYPES:
            assert applicability_confidence_basis(a, t, "yes")["label"] != "low"


# ── INV-A: orthogonality — THE acceptance ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a", _ADDRESSEES)
@pytest.mark.parametrize("t", _OBLIGATION_TYPES)
@pytest.mark.parametrize("b", _BINDS)
def test_extraction_confidence_is_orthogonal_over_full_grid(a, b, t):
    """INV-A (cardinal acceptance): for EVERY (addressee, type, binds), the label is byte-identical across the
    whole extraction-confidence range incl. the extremes and None. Extraction quality ⟂ legal-solidity label."""
    baseline = applicability_confidence_basis(a, t, b, extraction_confidence=None)["label"]
    for ec in _EXTRACTION_CONFS:
        got = applicability_confidence_basis(a, t, b, extraction_confidence=ec)["label"]
        assert got == baseline, (
            f"extraction_confidence={ec} changed the label for ({a},{t},{b}): {got} != {baseline}"
        )


def test_extraction_confidence_zero_does_not_lower_high_label():
    """The sharpest form of INV-A: a perfect (HIGH) verdict at extraction conf 0.0 STAYS high — a noisy
    extraction can never demote a categorical/solid duty (the false-negative this module exists to prevent)."""
    for a, t, b in [("aifm", "substantive_duty", "yes"), ("any_person", "generic_offence", "yes")]:
        assert applicability_confidence_basis(a, t, b, extraction_confidence=0.0)["label"] == "high"
        assert applicability_confidence_basis(a, t, b, extraction_confidence=1.0)["label"] == "high"


def test_extraction_confidence_only_appears_in_reason_not_label():
    """When supplied, extraction_confidence is recorded in the audit reason — but the label is unchanged."""
    without = applicability_confidence_basis(
        "aifm", "substantive_duty", "yes", extraction_confidence=None
    )
    with_ec = applicability_confidence_basis(
        "aifm", "substantive_duty", "yes", extraction_confidence=0.1
    )
    assert without["label"] == with_ec["label"]  # label untouched
    assert "0.1" in with_ec["reason"]  # value shown for audit only
    assert "0.1" not in without["reason"]


# ── INV-B: generic offences are not extraction-fragile (the s.614 case) ────────────────────────────────────
def test_gold_s614_generic_offences_are_high_despite_low_extraction_conf():
    """The 13 ZISIF s.614 generic offences carry extraction conf ~0.899 yet bind the AIFM categorically: they
    MUST get a HIGH (never low) applicability label — proving extraction conf does not demote them."""
    role = project_entity_role("aifm", "EU AIFM marketing its own AIF")
    seen = 0
    for f in _gold_findings():
        p = f.get("payload") or {}
        addr = addressee_class(p.get("actor"))
        otype = obligation_type(addr, p.get("modal"), f.get("statement"))
        if otype != "generic_offence" and addr != "any_person":
            continue
        seen += 1
        verdict = decide_binding(
            role, addr, otype, binds_confidence=float(f.get("confidence") or 1.0)
        )
        out = applicability_confidence_basis(
            addr,
            otype,
            verdict["binds"],
            extraction_confidence=f.get("confidence"),
            project_role=role,
        )
        assert out["label"] == "high", f"s.614 generic offence got non-high label {out['label']!r}"
        assert out["label"] != "low"
        # confirm these are the noisy-extraction rows the spec calls out
        assert abs(float(f.get("confidence")) - 0.9) < 0.05
    assert seen == 13, f"expected the 13 s.614 generic offences in the gold slice, saw {seen}"


# ── INV-C: the floor is KEEP-MORE only, and bars extraction confidence ─────────────────────────────────────
def test_floor_zero_is_inert():
    """floor_value <= 0 never demotes (the default-OFF state): no label strength is below 0."""
    for lab in _LABELS:
        assert applicability_confidence_floor(lab, 0) is False
        assert applicability_confidence_floor(lab, -1) is False


def test_floor_demotes_only_strictly_weaker_labels():
    """The floor fires (True = demote out_of_scope → conditional) iff the label strength is strictly below it.
    Strength ordinal: low=0 < moderate=1 < high=2."""
    # floor_value=1 → only 'low' (strength 0) is below it
    assert applicability_confidence_floor("low", 1) is True
    assert applicability_confidence_floor("moderate", 1) is False
    assert applicability_confidence_floor("high", 1) is False
    # floor_value=2 → 'low' and 'moderate' are below it
    assert applicability_confidence_floor("low", 2) is True
    assert applicability_confidence_floor("moderate", 2) is True
    assert applicability_confidence_floor("high", 2) is False
    # floor_value=3 → everything is below it (aggressive keep-more)
    assert all(applicability_confidence_floor(lab, 3) is True for lab in _LABELS)


def test_floor_signature_bars_extraction_confidence():
    """SAFETY: the floor function cannot read extraction confidence — it is not a parameter. Passing it must
    raise, proving a noisy extraction can never trigger (or block) a demotion."""
    import inspect

    params = list(inspect.signature(applicability_confidence_floor).parameters)
    assert params == ["label", "floor_value"], (
        f"floor signature must bar extraction_confidence, got {params}"
    )
    with pytest.raises(TypeError):
        applicability_confidence_floor("low", 1, extraction_confidence=0.1)  # type: ignore[call-arg]


def test_floor_is_keep_more_only_simulated_application():
    """Apply the floor exactly as a caller must: it may turn out_of_scope → conditional (KEEP MORE) and MUST
    NEVER turn yes/conditional → out_of_scope. Simulate the keep-more contract across the whole label set."""

    def apply_floor(binds: str, label: str, floor_value: int) -> str:
        # The ONLY sanctioned use of the floor: demote an out_of_scope to conditional. Never the reverse.
        if binds == "out_of_scope" and applicability_confidence_floor(label, floor_value):
            return "conditional"
        return binds

    for label in _LABELS:
        for floor_value in (0, 1, 2, 3):
            # a kept verdict is never made out_of_scope by the floor
            assert apply_floor("yes", label, floor_value) == "yes"
            assert apply_floor("conditional", label, floor_value) == "conditional"
            # an out_of_scope is only ever surfaced (to conditional) — never deepened
            result = apply_floor("out_of_scope", label, floor_value)
            assert result in ("out_of_scope", "conditional")


# ── INV-C: this module changes no binding verdict (binding_recall stays 1.0) ───────────────────────────────
def test_module_changes_no_binding_verdict_over_gold():
    """WF-2 only annotates. Confirm that over all 79 gold rows the binds verdict from WF-1 is untouched by the
    confidence-split call (the call returns a label dict; it does not — and cannot — return a binds verdict)."""
    role = project_entity_role("aifm", "EU AIFM marketing its own AIF")
    for f in _gold_findings():
        p = f.get("payload") or {}
        addr = addressee_class(p.get("actor"))
        otype = obligation_type(addr, p.get("modal"), f.get("statement"))
        verdict = decide_binding(
            role, addr, otype, binds_confidence=float(f.get("confidence") or 1.0)
        )
        out = applicability_confidence_basis(
            addr,
            otype,
            verdict["binds"],
            extraction_confidence=f.get("confidence"),
            project_role=role,
        )
        # the WF-2 result carries NO 'binds' key — it is structurally incapable of changing the verdict
        assert "binds" not in out
        assert set(out) == {"basis", "label", "reason"}


# ── the eval against the 79 gold findings ──────────────────────────────────────────────────────────────────
def test_eval_runs_and_passes_acceptance():
    report = wf2.run()
    assert report["n"] == 79
    for cfg in ("no_exclude", "pure_aifm"):
        c = report[cfg]
        assert c["n_obligations"] == 79
        # CARDINAL: WF-2 changes no verdict.
        assert c["binding_recall"] == 1.0, f"{cfg}: binding_recall must stay 1.0"
        # THE acceptance: extraction_confidence is provably orthogonal to the label.
        assert c["extraction_orthogonal"] is True, (
            f"{cfg}: label is not orthogonal to extraction_confidence"
        )
        # the 13 s.614 offences are HIGH/non-low despite their ~0.899 extraction conf
        assert c["n_generic_offences"] == 13
        assert c["generic_offence_non_low"] is True
        assert c["generic_offence_all_high"] is True
    assert report["ac_pass"] is True


def test_eval_label_distribution_is_sensible():
    """A sensible spread: the direct AIFM duties + s.614 offences anchor HIGH; the transposed/counterparty/
    licence conditionals anchor MODERATE; and (with two unparseable CZ prospectus cross-refs) a small LOW tail.
    No single label swamps the whole set."""
    report = wf2.run()
    dist = report["no_exclude"]["label_dist"]
    assert dist.get("high", 0) > 0, "expected some HIGH (direct AIFM duties + s.614 offences)"
    assert dist.get("moderate", 0) > 0, (
        "expected some MODERATE (transposed/counterparty/licence conditionals)"
    )
    # the distribution sums to all 79 rows and no label monopolises everything
    assert sum(dist.values()) == 79
    assert max(dist.values()) < 79


def test_eval_basis_distribution_covers_expected_grounds():
    """The gold slice should exercise the key bases: addressee_match (AIFM duties), generic_offence_coverage
    (the 13 s.614), transposition_vehicle (member_state_requires_entity), and the conditional grounds."""
    report = wf2.run()
    basis = report["no_exclude"]["basis_dist"]
    assert basis.get("addressee_match", 0) > 0
    assert basis.get("generic_offence_coverage", 0) == 13
    assert basis.get("transposition_vehicle", 0) > 0
    assert (
        basis.get("counterparty_contractual", 0)
        + basis.get("licence_scope_presumed", 0)
        + basis.get("conditional_assignment", 0)
    ) > 0


def test_eval_determinism():
    """Pure + deterministic: two runs of the eval produce byte-identical reports."""
    assert wf2.run() == wf2.run()
