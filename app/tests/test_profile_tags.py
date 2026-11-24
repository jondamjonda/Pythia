"""B2a-07 — SME business-profile tag helper (src/regsuper/analysis/profile_tags.py).

`derive_tags_for_profile(profile)` maps a `BusinessProfile` (or any duck-typed
object with the same fields) onto the SAME controlled tag vocabularies the
financial lane uses (`SECTOR_TO_TAGS` / `ENTITY_TO_ACTIVITY` in l0_intake),
deterministically and with the same unknown-value passthrough + order-
preserving dedup discipline as `derive_tags`.

This suite pins two things:

  * the SME mapping produces the expected controlled tags for a handful of
    representative profiles (sro+NACE, data-controller gate, landlord/
    developer-style profile, taxpayer activity, unknown passthrough, explicit
    pre-derived tag merge);
  * a BYTE-PARITY GUARD: importing the new module does NOT perturb
    `l0_intake.derive_tags` — it still returns the documented output for a
    couple of `ProjectIntake` fixtures (proving the protected financial path
    is untouched by the new module's import).

Everything is offline: pure functions, no DB, no LLM, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

from regsuper.analysis.l0_intake import ProjectIntake, derive_tags
from regsuper.analysis.profile_tags import derive_tags_for_profile


def _profile(**overrides):
    """Duck-typed BusinessProfile stub with the documented field defaults."""
    base = dict(
        legal_form="other",
        nace_code=None,
        sector_override=None,
        additional_activities=[],
        regulated_activity=None,
        processes_personal_data=False,
        handles_client_money=False,
        has_real_estate=False,
        cross_border=False,
        is_employer=False,
        derived_sector_tags=[],
        derived_activity_tags=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# SME mapping — representative profiles
# ---------------------------------------------------------------------------
def test_sro_with_nace_section_maps_to_controlled_tags():
    # An s.r.o. manufacturer (NACE C25). legal_form → commercial/corporate;
    # NACE section C → manufacturing → SECTOR_TO_TAGS["manufacturing"].
    sector_tags, activity_tags = derive_tags_for_profile(
        _profile(legal_form="sro", nace_code="C25")
    )
    assert sector_tags == ["manufacturing", "product_safety", "environmental"]
    assert activity_tags == ["commercial", "corporate"]


def test_data_controller_gate_injects_data_protection():
    # The processes_personal_data gate → data_protection activity tag.
    _, activity_tags = derive_tags_for_profile(
        _profile(legal_form="sro", processes_personal_data=True)
    )
    assert "data_protection" in activity_tags
    # Gate tag is appended after the legal-form tags, in declared order.
    assert activity_tags == ["commercial", "corporate", "data_protection"]


def test_landlord_developer_style_profile():
    # A real-estate developer: NACE section L (real_estate) + the has_real_estate
    # gate + a 'developer' additional activity (known ENTITY_TO_ACTIVITY key).
    sector_tags, activity_tags = derive_tags_for_profile(
        _profile(
            legal_form="sro",
            nace_code="L68",
            has_real_estate=True,
            additional_activities=["developer"],
        )
    )
    # SECTOR_TO_TAGS["real_estate"] then the gate's real_estate (deduped).
    assert sector_tags == ["real_estate", "leasing", "construction", "consumer_protection"]
    # legal form, gate (real_property), then developer → construction/real_property/environmental.
    assert activity_tags == [
        "commercial",
        "corporate",
        "real_property",
        "construction",
        "environmental",
    ]


def test_taxpayer_activity_maps_through_entity_vocab():
    # A 'taxpayer' regulated_activity routes through ENTITY_TO_ACTIVITY.
    _, activity_tags = derive_tags_for_profile(
        _profile(legal_form="osvč", regulated_activity="taxpayer")
    )
    assert activity_tags == ["self_employed", "commercial", "tax", "accounting"]


def test_unknown_values_pass_through_normalised():
    # Unknown legal form / NACE section / activity are kept as normalised
    # tokens, never dropped — mirrors derive_tags' entity passthrough.
    sector_tags, activity_tags = derive_tags_for_profile(
        _profile(
            legal_form="Weird Form",
            nace_code="Z99",
            regulated_activity="Some Niche Thing",
        )
    )
    assert sector_tags == ["Z"]  # unknown NACE section letter, kept upper-case
    assert activity_tags == ["weird_form", "some_niche_thing"]


def test_explicit_pre_derived_tags_are_respected_and_deduped():
    # Tags already on the profile are merged (appended last), order-preserving,
    # and deduped against the freshly-derived ones.
    sector_tags, activity_tags = derive_tags_for_profile(
        _profile(
            legal_form="sro",
            nace_code="C25",
            derived_sector_tags=["manufacturing", "bespoke_sector"],
            derived_activity_tags=["corporate", "bespoke_activity"],
        )
    )
    # "manufacturing" already present (deduped), "bespoke_sector" appended.
    assert sector_tags == [
        "manufacturing",
        "product_safety",
        "environmental",
        "bespoke_sector",
    ]
    assert activity_tags == ["commercial", "corporate", "bespoke_activity"]


def test_partial_stub_with_missing_attrs_is_safe():
    # A minimal duck-typed object (only legal_form) must not raise — missing
    # attrs default to empty/false.
    sector_tags, activity_tags = derive_tags_for_profile(SimpleNamespace(legal_form="as"))
    assert sector_tags == []
    assert activity_tags == ["commercial", "corporate"]


def test_output_is_deterministic_across_calls():
    p = _profile(legal_form="sro", nace_code="K64", handles_client_money=True)
    assert derive_tags_for_profile(p) == derive_tags_for_profile(p)


# ---------------------------------------------------------------------------
# BYTE-PARITY GUARD — the financial lane is untouched by this module's import
# ---------------------------------------------------------------------------
def test_derive_tags_byte_parity_fin_services():
    # Documented derive_tags output for a fin_services AIFM intake — importing
    # profile_tags (done at module top) must not perturb it.
    sector_tags, activity_tags = derive_tags(
        ProjectIntake(
            name="Fund X",
            sector="fin_services",
            jurisdiction_scope=["CZ", "EU"],
            entity_type="aifm",
        )
    )
    assert sector_tags == [
        "fin_services",
        "investment_funds",
        "capital_markets",
        "banking",
        "aml",
        "consumer_credit",
        "payments",
    ]
    # derive_tags appends the unrecognised entity key ('aifm' is not itself an
    # activity tag) as a passthrough token — this is its DOCUMENTED behaviour.
    assert activity_tags == [
        "fund_management",
        "marketing",
        "passporting",
        "risk_management",
        "aifm",
    ]


def test_derive_tags_byte_parity_unknown_passthrough():
    # Documented derive_tags output for an unmapped sector + unmapped entity:
    # both pass through as normalised tokens.
    sector_tags, activity_tags = derive_tags(
        ProjectIntake(
            name="Niche Co",
            sector="Space Mining",
            jurisdiction_scope=["CZ"],
            entity_type="Asteroid Operator",
        )
    )
    assert sector_tags == ["space_mining"]
    assert activity_tags == ["asteroid_operator"]
