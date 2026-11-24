"""B2 — the SME role vocabulary that fixes WF-1's inertness for SMEs.

Proves the contract:
  * The vocab table is a PURE, lawyer-reviewable constant (no DB / LLM / randomness).
  * FLAG OFF ⇒ `_resolve_role` is byte-identical to the pre-B2 finance-only behaviour (the WF-1 79-gold
    surface — every finance project — is untouched).
  * FLAG ON ⇒ an SME `sro` employer resolves to a real SME role (`sme_limited_company` + `sme_employer`), so
    WF-1 is no longer inert for SMEs.
  * DEMOTE-NEVER-DROP holds by construction: an SME role can only DEMOTE a finance obligation to `conditional`,
    never `out_of_scope` — binding_recall stays 1.0.

The DB-touching scoring pass mocks the session seam exactly like test_applicability_wiring (no DB, no network).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from regsuper.analysis.applicability import decide_binding, project_entity_role
from regsuper.analysis.sme_roles import (
    _LEGAL_FORM_EXACT,
    _NACE_PREFIX_ROLES,
    SmeProfileFacts,
    resolve_sme_roles,
    sme_role_dict,
)


# ── the vocab table is a pure lawyer-reviewable constant ────────────────────────────────────────────────────
def test_vocab_table_is_pure_constant():
    """Calling the resolver twice with the same facts yields the SAME result (deterministic, no state)."""
    facts = SmeProfileFacts(
        legal_form="sro", is_employer=True, processes_personal_data=True, nace_code="10.71"
    )
    assert resolve_sme_roles(facts) == resolve_sme_roles(facts)
    # the tables are plain constants
    assert isinstance(_LEGAL_FORM_EXACT, dict) and "sro" in _LEGAL_FORM_EXACT
    assert all(isinstance(pfx, str) for pfx, _role in _NACE_PREFIX_ROLES)


def test_empty_facts_resolve_to_no_roles():
    """A profile with no facts → [] → assumed role (fail-safe keep), i.e. still inert for empty profiles."""
    assert resolve_sme_roles(SmeProfileFacts()) == []
    d = sme_role_dict(SmeProfileFacts())
    assert d["primary"] == "unknown" and d["assumed"] is True


# ── legal_form → base role ──────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "legal_form,expected",
    [
        ("sro", "sme_limited_company"),
        ("s.r.o.", "sme_limited_company"),
        ("společnost s ručením omezeným", "sme_limited_company"),
        ("osvč", "sme_self_employed"),
        ("živnostník", "sme_self_employed"),
        ("as", "sme_joint_stock_company"),
        ("akciová společnost", "sme_joint_stock_company"),
        ("družstvo", "sme_cooperative"),
    ],
)
def test_legal_form_base_role(legal_form, expected):
    roles = resolve_sme_roles(SmeProfileFacts(legal_form=legal_form))
    assert roles == [expected]


# ── the five gates each ADD a role ──────────────────────────────────────────────────────────────────────────
def test_gates_add_roles_additively():
    facts = SmeProfileFacts(
        legal_form="sro",
        is_employer=True,
        processes_personal_data=True,
        handles_client_money=True,
        has_real_estate=True,
        cross_border=True,
    )
    roles = resolve_sme_roles(facts)
    assert roles[0] == "sme_limited_company"
    assert set(roles) == {
        "sme_limited_company",
        "sme_employer",
        "sme_data_controller",
        "sme_client_money_handler",
        "sme_real_estate_operator",
        "sme_cross_border_trader",
    }


def test_false_gates_add_nothing():
    roles = resolve_sme_roles(SmeProfileFacts(legal_form="sro"))
    assert roles == ["sme_limited_company"]


# ── NACE → activity role ────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "nace,expected",
    [
        ("10.71", "sme_food_business"),
        ("56.10", "sme_food_business"),
        ("49.41", "sme_carrier"),
        ("52.29", "sme_carrier"),
    ],
)
def test_nace_activity_role(nace, expected):
    roles = resolve_sme_roles(SmeProfileFacts(legal_form="sro", nace_code=nace))
    assert expected in roles


def test_unlisted_nace_adds_nothing():
    roles = resolve_sme_roles(
        SmeProfileFacts(legal_form="sro", nace_code="62.01")
    )  # software — not listed
    assert roles == ["sme_limited_company"]


# ── the role dict shape matches project_entity_role (so decide_binding consumes it unchanged) ───────────────
def test_role_dict_shape_matches_project_entity_role():
    d = sme_role_dict(SmeProfileFacts(legal_form="sro", is_employer=True))
    finance = project_entity_role("aifm")
    # the keys decide_binding reads must all be present with safe values
    for k in ("primary", "excludes", "has_top_up", "assumed"):
        assert k in d and k in finance
    assert d["primary"] == "sme_limited_company"
    assert (
        d["excludes"] == []
    )  # NEVER a finance exclusion → declared_exclusion out_of_scope unreachable
    assert d["has_top_up"] is True
    assert d["assumed"] is False
    assert d["source"] == "sme_profile"


# ── DEMOTE-NEVER-DROP: an SME role can only demote a finance obligation, never out_of_scope ─────────────────
def test_sme_role_demotes_finance_obligation_never_out_of_scope():
    """An SME s.r.o. employer facing a finance-addressed obligation (AIFM/MiFID) → conditional, never dropped."""
    role = sme_role_dict(SmeProfileFacts(legal_form="sro", is_employer=True))
    # MiFID firm-conduct duty (a finance addressee, substantive) → the WORST an SME role can do is demote
    v = decide_binding(role, "mifid_investment_firm", "substantive_duty")
    assert v["binds"] == "conditional"  # demoted, NOT out_of_scope
    assert v["binds"] != "out_of_scope"
    # even a pure legislature transposition line cannot be set aside on an SME role's basis except via the
    # member_state path (which is addressee-driven, not role-driven) — an entity duty stays kept.
    v2 = decide_binding(role, "aifm", "substantive_duty")
    assert v2["binds"] == "conditional"


def test_sme_role_never_produces_declared_exclusion_out_of_scope():
    """The declared_exclusion out_of_scope path needs mifid_investment_firm ∈ excludes; an SME role sets none."""
    role = sme_role_dict(SmeProfileFacts(legal_form="sro"))
    v = decide_binding(role, "mifid_investment_firm", "substantive_duty")
    assert v["binds"] == "conditional" and v["oos_basis"] is None


# ── the scoring-pass wiring (mock DB) ───────────────────────────────────────────────────────────────────────
def _model_aware_scoring_session(findings, project, profile):
    """A session whose `get` returns the right object by model class name (Project vs BusinessProfile)."""

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: findings)

    class _Session:
        async def get(self, model, _pid):
            name = getattr(model, "__name__", "")
            if name == "BusinessProfile":
                return profile
            return project

        async def execute(self, *_a, **_k):
            return _Result()

    @asynccontextmanager
    async def _scope():
        yield _Session()

    return _scope


async def _fake_emit(*_a, **_k):
    return None


def _scoring_finding(actor, modal="shall", conf=1.0, statement=None):
    return SimpleNamespace(
        statement=statement or f"{actor} shall do X",
        confidence_overall=conf,
        finding_type="obligation",
        payload={"actor": actor, "modal": modal},
    )


async def _run_scoring(findings, project, profile, *, sme_flag, floor=0.0):
    from regsuper.analysis import applicability_scoring as sc

    with (
        patch.object(sc, "session_scope", _model_aware_scoring_session(findings, project, profile)),
        patch.object(sc, "emit", _fake_emit),
        patch.object(
            sc,
            "get_settings",
            lambda: SimpleNamespace(
                entity_scoping_enabled=True,
                entity_scoping_floor=floor,
                applicability_confidence_enabled=False,
                sme_roles_enabled=sme_flag,
            ),
        ),
    ):
        return await sc.run_applicability_scoring(uuid4(), force=False)


def _sme_project():
    """A project linked to a business_profile, entity_type=legal_form (as api/profiles.py builds it) → the
    finance vocab returns primary='unknown' (the exact SME inertness case)."""
    return SimpleNamespace(
        metadata_={},
        entity_type="sro",
        declared_intent=None,
        business_profile_id=uuid4(),
    )


def _sme_employer_profile():
    return SimpleNamespace(
        legal_form="sro",
        nace_code=None,
        processes_personal_data=False,
        handles_client_money=False,
        has_real_estate=False,
        cross_border=False,
        is_employer=True,
    )


async def test_flag_off_resolve_role_unchanged_for_sme():
    """FLAG OFF ⇒ the SME lane is never consulted ⇒ an SME `sro` still resolves to the finance 'unknown' role,
    byte-identical to pre-B2 (WF-1 stays inert — the fail-safe guard keeps everything at 'yes')."""
    project = _sme_project()
    profile = _sme_employer_profile()
    f = _scoring_finding("AIFMs")  # finance obligation
    report = await _run_scoring([f], project, profile, sme_flag=False)
    # unknown finance role → guard keeps everything at 'yes'
    assert f.payload["applicability"]["binds"] == "yes"
    assert report.project_role == "unknown"


async def test_flag_on_sme_sro_employer_resolves_role():
    """FLAG ON ⇒ the SME `sro` employer resolves to the real SME role (sme_limited_company) via the profile
    bridge — WF-1 is no longer inert for SMEs."""
    project = _sme_project()
    profile = _sme_employer_profile()
    f = _scoring_finding("AIFMs")  # a finance obligation, now facing an SME role
    report = await _run_scoring([f], project, profile, sme_flag=True)
    assert report.project_role == "sme_limited_company"
    # DEMOTE-NEVER-DROP: the finance obligation is demoted to conditional, NOT dropped/out_of_scope
    assert f.payload["applicability"]["binds"] == "conditional"


async def test_flag_on_empty_profile_stays_fail_safe():
    """FLAG ON but an all-empty profile ⇒ the SME lane resolves nothing ⇒ the finance 'assumed' role stands ⇒
    everything kept (still inert for a profile with no facts, exactly as today)."""
    project = _sme_project()
    empty_profile = SimpleNamespace(
        legal_form=None,
        nace_code=None,
        processes_personal_data=False,
        handles_client_money=False,
        has_real_estate=False,
        cross_border=False,
        is_employer=False,
    )
    f = _scoring_finding("AIFMs")
    report = await _run_scoring([f], project, empty_profile, sme_flag=True)
    assert report.project_role == "unknown"
    assert f.payload["applicability"]["binds"] == "yes"


async def test_flag_on_finance_entity_keeps_finance_role():
    """FLAG ON must NOT perturb a genuine finance project: an aifm entity keeps its finance role even with a
    linked profile (the SME lane only fires when the finance role is 'assumed'). Guards the WF-1 79-gold."""
    project = SimpleNamespace(
        metadata_={},
        entity_type="aifm",
        declared_intent=None,
        business_profile_id=uuid4(),
    )
    profile = (
        _sme_employer_profile()
    )  # a profile exists, but must be ignored (finance role not assumed)
    f = _scoring_finding("AIFMs")
    report = await _run_scoring([f], project, profile, sme_flag=True)
    assert report.project_role == "aifm"
    assert f.payload["applicability"]["binds"] == "yes"
