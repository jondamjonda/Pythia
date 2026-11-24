"""Entity-role applicability: does an extracted obligation actually bind this client?

A Czech advocate (ČAK) confirmed all 79 obligations the cascade produced for an "EU AIFM marketing
its own AIF" as faithful restatements of current law, yet only about 31 actually bind a pure AIFM.
The rest were faithful but out of scope: MiFID duties for investment firms, counterparty duties,
"Member States shall..." transposition lines, generic offences, licence-scope rules. The cascade had
conflated "this provision is in the applicable corpus" with "this provision binds this entity in
this role".

This module is the pure, deterministic, lawyer-reviewable core of the fix. It reads the
already-extracted ``finding.payload['actor']`` (no LLM call and no prompt change, so the L2/L3
prompt-hash audit chain is untouched), classifies the obligation's addressee and deontic type, and
decides whether it binds the project's entity role; ``applicability_scoring`` applies it.

Hiding a genuinely binding duty (a false negative) is worse than showing one that does not apply (a
false positive): a missed gap costs far more than a spurious one. ``decide_binding`` is therefore
fail-safe. Its guard can only weaken a verdict toward "yes", and only two provably safe classes are
ever eligible for ``out_of_scope``: transposition lines addressed to the legislature, and a role
exclusion the client declared (with no top-up authorisation). Everything else is at most
``conditional``: demoted and explained, never dropped.
"""

from __future__ import annotations

import re
from typing import Literal

# Project side: who the client is.
EntityRole = Literal[
    "aifm",
    "ucits_manco",
    "mifid_investment_firm",
    "credit_institution",
    "depositary",
    "external_valuer",
    "prime_broker",
    "insurance_undertaking",
    "casp",
    "payment_institution",
    "self_managed_aif",
    "unknown",
]

# Provision side: who the norm binds. A superset of EntityRole plus the non-entity addressees seen
# in the lawyer-reviewed sample.
AddresseeClass = Literal[
    "aifm",
    "ucits_manco",
    "mifid_investment_firm",
    "credit_institution",
    "depositary",
    "external_valuer",
    "prime_broker",
    "investment_company_licence",  # ZISIF s.11/s.38 activity scope, conditional on the licence
    "member_state",  # "Member States shall/may..." transposition instruction
    "any_person",  # ZISIF s.614 generic offence; binds everyone, AIFMs included
    "prospectus_responsible",  # ZPKT s.34 co-drafter or guarantor liability
    "unknown",
]

# Deontic class of the norm. This second axis is essential: the addressee alone cannot tell a duty
# that binds the entity from an instruction to the legislature.
ObligationType = Literal[
    "substantive_duty",
    # "Member States shall adopt laws...": addressed only to the legislature (safe to set aside).
    "transposition_instruction",
    # "Member States shall require that AIFMs...": a vehicle whose substance binds the entity.
    "member_state_requires_entity",
    # "Member States may authorise...": a national discretion; relevant context, not a duty.
    "member_state_discretion",
    "generic_offence",
    "licence_scope_rule",
    "counterparty_duty",
    "liability_allocation",
    "unknown",
]

Binds = Literal["yes", "conditional", "out_of_scope"]


# Actor string to addressee class. Actor strings are free text in English or Czech; rules are
# case-insensitive substring matches, ordered most specific first.
_ADDRESSEE_RULES: tuple[tuple[str, AddresseeClass], ...] = (
    ("prime broker", "prime_broker"),
    ("external valuer", "external_valuer"),
    ("depositar", "depositary"),  # depositary / depozitář
    ("member state", "member_state"),
    ("ručitel", "prospectus_responsible"),
    (
        "prospekt",
        "prospectus_responsible",
    ),  # osoba, která sestavila prospekt / osoby uvedené v odstavci 1
    ("administrator of an investment fund", "investment_company_licence"),
    ("administrátor", "investment_company_licence"),
    ("investiční společnost", "investment_company_licence"),
    ("investment company", "investment_company_licence"),
    ("§ 481", "investment_company_licence"),  # foreign entity with permission under § 481
    ("ucits", "ucits_manco"),
    ("aifm", "aifm"),  # AIFM / AIFMs
    ("investment firm", "mifid_investment_firm"),
    ("credit institution", "credit_institution"),
    (
        "legal or entrepreneurial natural person",
        "any_person",
    ),  # the ZISIF s.614 generic-offence addressee
    ("any person", "any_person"),
    # Deliberately no bare "osoba" rule: in the reviewed sample the only Czech "osoba..." strings
    # are prospectus cross-references ("osoba uvedená v odstavci 1"), which a generic "osoba" to
    # any_person rule would misclassify as a generic offence. Unmatched Czech person strings fall to
    # "unknown", which the guard keeps. Precise handling of those cross-references would need the
    # provision's context.
)


def addressee_class(actor: str | None, deontic_actor: str | None = None) -> AddresseeClass:
    """Normalise the L3-extracted obligation actor into a closed addressee class.

    The actor is free text in Czech or English. An unrecognised actor returns ``"unknown"``, which
    the binding guard treats fail-safe (keep). ``deontic_actor`` (the provision column, absent on
    the nexus corpus) is only a secondary corroboration.
    """
    for hay in (actor, deontic_actor):
        if not hay:
            continue
        h = hay.lower()
        for needle, cls in _ADDRESSEE_RULES:
            if needle in h:
                return cls
    return "unknown"


# Obligation-type classifier: deterministic and driven by the obligation statement.
# A pure transposition instruction tells the legislature to enact law (safe to set aside). It is
# distinguished from "Member States shall require that <entity> ...", a delivery vehicle whose
# substance binds the entity. Word-boundary regexes (not bare substrings) keep "adopt" inside a
# longer word from matching.
#
# Safety gate, do not weaken: `transposition_instruction` is the only type the binding matrix can
# route to `out_of_scope`, and it is reachable only for a `member_state` addressee. An entity duty
# (AIFM, investment firm, ...) whose statement happens to contain "adopt" or "ensure" (for example
# "shall adopt a remuneration policy") can never be classified as transposition, so it can never be
# hidden. Binding recall stays at 1.0 by construction.
_ADOPT_LAW_RX = re.compile(
    r"\b(adopt|lay down|bring into force|necessary to comply|transpos\w*|by way of legislation)\b"
    r"|laws[, ]+regulations"
)
_REQUIRES_ENTITY_RX = re.compile(r"\b(require[sd]?\s+(that|the)|ensure\s+that|provide\s+that)\b")
# Liability allocation (ZPKT s.34, prospectus liability): a liability verb plus a prospectus or data
# object. Only used to reclassify an ambiguous or prospectus addressee, never to downgrade a named
# entity's substantive duty.
_PROSPECTUS_LIABILITY_RX = re.compile(
    r"odpovídat za (správnost|obsah|úplnost|údaj)"
    r"|liable for|responsible for the (content|accuracy|completeness)|prospekt|prospectus"
)
# Generic offence (ZISIF s.614): an offence-defining statement binds any person, the AIFM included,
# so it is never demoted.
_GENERIC_OFFENCE_RX = re.compile(
    r"dopustí se přestupku|se dopustí|commits an offen[cs]e|offen[cs]e to"
)


def obligation_type(
    addressee: AddresseeClass, modal: str | None, statement: str | None = None
) -> ObligationType:
    """Classify the obligation's deontic type deterministically.

    The statement and modal are consulted so the type can correct mistakes an addressee-only
    reading would make, while the one out-of-scope-eligible type (``transposition_instruction``)
    stays reachable only through a ``member_state`` addressee. No LLM and no randomness.
    """
    m = (modal or "").lower().strip()
    s = statement or ""

    # 1) A Member State addressee is the only path to transposition_instruction.
    if addressee == "member_state":
        if m == "may":
            return "member_state_discretion"
        if _REQUIRES_ENTITY_RX.search(
            s
        ):  # "shall require/ensure that <entity> ...": the substance binds (kept)
            return "member_state_requires_entity"
        if _ADOPT_LAW_RX.search(s):  # pure "adopt laws / necessary to comply": legislature only
            return "transposition_instruction"
        return "member_state_requires_entity"  # ambiguous: fall back to the binding (kept) reading

    # 2) Generic offence: binds any person, the AIFM included (never demoted).
    if addressee == "any_person" or _GENERIC_OFFENCE_RX.search(s):
        return "generic_offence"

    # 3) Liability allocation: reclassify only an ambiguous or prospectus actor, never an entity's
    # own duty.
    if addressee == "prospectus_responsible":
        return "liability_allocation"
    if addressee == "unknown" and _PROSPECTUS_LIABILITY_RX.search(s):
        return "liability_allocation"

    # 4) Counterparty duty versus licence-scoped activity, decided by the addressee.
    if addressee in ("depositary", "external_valuer", "prime_broker"):
        return "counterparty_duty"
    if addressee == "investment_company_licence":
        return "licence_scope_rule"

    # 5) The regulated entity itself: a substantive duty.
    if addressee in ("aifm", "ucits_manco", "mifid_investment_firm", "credit_institution"):
        return "substantive_duty"
    return "unknown"


# Project entity-role derivation. Keys reuse the entity-type vocabulary of l0_intake's
# ENTITY_TO_ACTIVITY.
_ROLE_SYNONYMS: dict[str, EntityRole] = {
    "aifm": "aifm",
    "aif": "aifm",
    "alternative investment fund manager": "aifm",
    "ucits": "ucits_manco",
    "ucits manco": "ucits_manco",
    "management company": "ucits_manco",
    "investment_firm": "mifid_investment_firm",
    "investment firm": "mifid_investment_firm",
    "mifid": "mifid_investment_firm",
    "credit_institution": "credit_institution",
    "credit institution": "credit_institution",
    "bank": "credit_institution",
    "depositary": "depositary",
    "external_valuer": "external_valuer",
    "prime_broker": "prime_broker",
    "insurance": "insurance_undertaking",
    "insurance_undertaking": "insurance_undertaking",
    "casp": "casp",
    "payment_institution": "payment_institution",
    "self_managed_aif": "self_managed_aif",
}


def project_entity_role(entity_type: str | None, declared_intent: str | None = None) -> dict:
    """Derive the project's structured entity role from its entity type and declared intent.

    ``entity_type`` may be free text or an enum value. Returns a dict with ``primary``,
    ``excludes``, ``has_top_up``, ``assumed``, ``source`` and ``entity_role_version``. An unknown or
    empty type sets ``assumed=True`` so the gate keeps everything. ``has_top_up`` (whether an AIFMD
    Art 6(4) top-up authorisation lets MiFID duties reach the AIFM) is conservatively True unless
    the intent explicitly rules a top-up out.
    """
    raw = (entity_type or "").strip().lower()
    primary: EntityRole = "unknown"
    for k, role in _ROLE_SYNONYMS.items():
        if k in raw:
            primary = role
            break
    intent = (declared_intent or "").lower()
    # A "pure AIFM" is one whose client explicitly disclaims the MiFID investment-firm role.
    excludes: list[EntityRole] = []
    if primary == "aifm" and any(
        p in intent for p in ("not a mifid", "no mifid", "pure aifm", "not an investment firm")
    ):
        excludes.append("mifid_investment_firm")
    # A MiFID conduct duty can reach an AIFM through an Art 6(4) top-up authorisation. Assume it
    # might (MiFID stays in scope as 'conditional') unless the intent explicitly disclaims it.
    has_top_up = not any(
        p in intent for p in ("no top-up", "no top up", "without top-up", "no mifid top-up")
    )
    return {
        "primary": primary,
        "excludes": excludes,
        "has_top_up": has_top_up,
        "assumed": primary == "unknown",
        "source": "intake",
        "entity_role_version": 1,
    }


# The binding matrix and the over-suppression guard.
# `out_of_scope` is the only verdict that hides a finding from the binding view, so it must rest on
# a provably safe basis. Exactly two bases qualify; everything else is at most `conditional`
# (demoted and explained, never dropped):
#   * "legislature": the norm is addressed to the Member State. It can never bind a private entity,
#     so it is always safe to set aside.
#   * "declared_exclusion": the client declared a role exclusion (for example a pure AIFM) and no
#     top-up authorisation, so an addressee in the excluded role genuinely does not bind. Safe
#     because the client asserted the disqualifying fact; it is never inferred.
# Only a pure transposition_instruction ("Member States shall adopt laws") is legislature-eligible.
# "Shall require that <entity> ..." (member_state_requires_entity) and "may authorise ..."
# (member_state_discretion) carry or gate substantive entity duties, so they are held at
# `conditional` and never set aside.
_LEGISLATURE_TYPES = frozenset({"transposition_instruction"})


def decide_binding(
    role: dict,
    addressee: AddresseeClass,
    obl_type: ObligationType,
    binds_confidence: float = 1.0,
    floor: float = 0.0,
) -> dict:
    """Decide whether an obligation binds the project entity.

    Returns the ``payload['applicability']`` verdict dict. Fail-safe: the guard can only weaken a
    verdict toward "yes". Nothing is ever deleted; render decides how to show ``out_of_scope``. The
    confidence ``floor`` applies only to an ``out_of_scope`` verdict (a low-confidence set-aside is
    demoted to ``conditional``); ``yes`` and ``conditional`` are never floored, and ``floor=0.0``
    disables it.
    """
    primary: EntityRole = role.get("primary", "unknown")
    excludes = set(role.get("excludes") or [])
    assumed = bool(role.get("assumed"))
    # has_top_up gates the only entity-conduct out_of_scope basis (a declared MiFID exclusion). A
    # missing or explicit None value is ambiguous, not a declaration of "no top-up", so it defaults
    # to True (MiFID may still bind, so the finding stays conditional rather than hidden).
    # bool(None) would wrongly read as False.
    _top_up = role.get("has_top_up", True)
    has_top_up = True if _top_up is None else bool(_top_up)

    # 1) The matrix: the strict pre-guard verdict, a machine reason, and the out-of-scope basis.
    # Invariant the guard below relies on: the matrix sets `out_of_scope` on exactly two paths, the
    # legislature path and the declared-MiFID-exclusion path. Every other branch yields `yes` or
    # `conditional`. Any future out_of_scope path must set a matching `oos_basis` and be admitted in
    # the guard's `valid` check (step 2); otherwise it is demoted to conditional, not hidden.
    binds: Binds = "yes"
    reason = "addressee matches the project entity"
    oos_basis: str | None = None
    if obl_type in _LEGISLATURE_TYPES:
        binds, reason, oos_basis = (
            "out_of_scope",
            "addressed to the legislature (Member State) to adopt law, not the client",
            "legislature",
        )
    elif obl_type == "member_state_requires_entity":
        binds, reason = (
            "conditional",
            "transposed via your Member State — the substance binds the named entity; verify the national enactment",
        )
    elif obl_type == "member_state_discretion":
        binds, reason = (
            "conditional",
            "a Member-State discretion — decision-relevant context that shapes what binds you, not a direct duty",
        )
    elif addressee == "any_person":
        binds, reason = "yes", "generic offence — binds any person incl. the AIFM"
    elif addressee in ("depositary", "external_valuer", "prime_broker"):
        binds, reason = "conditional", "counterparty duty — verify / contract for it"
    elif obl_type == "licence_scope_rule":
        binds, reason = "conditional", "conditional on the project's licence scope"
    elif obl_type == "liability_allocation":
        binds, reason = "conditional", "conditional on the project's prospectus role"
    elif addressee == "mifid_investment_firm":
        if "mifid_investment_firm" in excludes and not has_top_up:
            binds, reason, oos_basis = (
                "out_of_scope",
                "client declared a pure AIFM with no MiFID top-up — MiFID firm-conduct does not bind",
                "declared_exclusion",
            )
        else:
            binds, reason = (
                "conditional",
                "reaches an AIFM only via an AIFMD Art 6(4) MiFID top-up authorisation",
            )
    elif primary != "unknown" and addressee not in (primary, "unknown"):
        binds, reason = (
            "conditional",
            f"addressed to '{addressee}', not the project role '{primary}'",
        )

    # 2) The guard can only weaken toward 'yes'. `guarded` records where the safety net fired.
    guarded = False
    if assumed or primary == "unknown":
        if binds != "yes":
            binds, reason, guarded, oos_basis = (
                "yes",
                "project entity-role unknown — keeping everything (fail-safe)",
                True,
                None,
            )
    elif addressee == "unknown":
        if binds != "yes":
            binds, reason, guarded, oos_basis = (
                "yes",
                "obligation addressee unparseable — keeping (fail-safe)",
                True,
                None,
            )
    # An out_of_scope verdict survives only on a still-valid safe basis; otherwise demote it.
    if binds == "out_of_scope":
        valid = (oos_basis == "legislature" and obl_type in _LEGISLATURE_TYPES) or (
            oos_basis == "declared_exclusion"
            and not assumed
            and addressee in excludes
            and not has_top_up
        )
        if not valid:
            binds, reason, guarded = (
                "conditional",
                reason + " (demoted: no valid out-of-scope basis)",
                True,
            )
        elif binds_confidence < floor:  # confidence floor (lawyer-tuned; 0.0 disables it)
            binds, reason, guarded = "conditional", reason + " (below applicability floor)", True

    return {
        "addressee_class": addressee,
        "obligation_type": obl_type,
        "binds": binds,
        "reason": reason,
        "oos_basis": oos_basis if binds == "out_of_scope" else None,
        "binds_confidence": round(float(binds_confidence), 4),
        "guarded": guarded,
        "project_primary": primary,
        "engine_version": 1,
    }
