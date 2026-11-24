"""Entity roles for small and medium businesses, so the applicability gate can work for them.

The role vocabulary in ``applicability.py`` (``_ROLE_SYNONYMS``) is entirely financial (aifm,
ucits, mifid, bank, depositary, ...). An ordinary Czech business, say an s.r.o. running a bakery
that employs people and processes personal data, has ``entity_type='sro'``, which matches none of
them. ``project_entity_role('sro')`` returns ``primary='unknown', assumed=True``, and
``decide_binding``'s fail-safe guard then keeps every obligation at ``binds='yes'``. For such
businesses the gate never demotes anything and the "does not bind you directly" section is always
empty, leaving an undifferentiated wall of obligations.

This module gives them a real role vocabulary. It is deterministic (no LLM and no prompt, so the
L2/L3 prompt-hash audit chain is untouched): a constant table plus pure functions, consulted only
when ``sme_roles_enabled`` is on (off by default). It does not edit ``_ROLE_SYNONYMS``, whose
behaviour the lawyer-verified 79-item gold set pins; it produces the same role-dict shape as
``project_entity_role``, so ``decide_binding``'s matrix and guard consume it unchanged.

Demotion, never deletion, holds by construction. The roles here (``sme_employer``,
``sme_vat_payer``, ...) are disjoint from every financial ``AddresseeClass``. For an obligation
with a financial addressee (or ``any_person``, ``member_state``, ``unknown``):

* no out_of_scope path is reachable: ``_LEGISLATURE_TYPES`` needs a ``member_state`` addressee,
  and the ``declared_exclusion`` path needs ``mifid_investment_firm`` in ``excludes``, which these
  roles never set;
* ``any_person`` gives ``yes``; Member State instructions and discretions, counterparty, licence
  and liability duties give ``conditional``; a financial addressee that is not the primary role
  falls to the final ``primary != 'unknown' and addressee not in (primary, 'unknown')`` branch and
  gives ``conditional``.

So at worst a finding is demoted to ``conditional``, which render shows in a visible section (L4
gap suppression is not wired here). Binding recall stays 1.0: no financial duty can be hidden.
Obligations whose addressee the classifier does not recognise (``zaměstnavatel``,
``plátce DPH``, ...) stay at ``yes``, so the result reads as "these bind you, these might".

All Czech copy here is a draft pending advocate review. The role table and the fact-to-role
mapping are the two parts for a lawyer to review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# The role space, disjoint from applicability.EntityRole and AddresseeClass. Each value is a closed
# role an ordinary Czech business can hold. Plain string Literals rather than an Enum, so the table
# is an easily diffed constant a lawyer can read top to bottom. These strings become the `primary`
# that decide_binding's matrix sees; being disjoint from every financial AddresseeClass, they can
# only demote (see the module docstring).
SmeRole = Literal[
    "sme_employer",  # zaměstnavatel: engages employees (Zákoník práce, ČSSZ, zdravotní pojištění)
    "sme_vat_payer",  # plátce DPH: VAT-registered (zákon o DPH)
    "sme_self_employed",  # OSVČ: self-employed person (živnostenský zákon, sociální/zdravotní)
    # s.r.o.: společnost s ručením omezeným (zákon o obchodních korporacích, účetnictví)
    "sme_limited_company",
    "sme_joint_stock_company",  # a.s.: akciová společnost (ZOK, povinný audit prahy)
    "sme_cooperative",  # družstvo (ZOK)
    "sme_data_controller",  # správce osobních údajů (GDPR / zákon č. 110/2019 Sb.)
    "sme_food_business",  # provozovatel potravinářského podniku (zákon o potravinách, NAŘÍZENÍ 178/2002)
    "sme_carrier",  # přepravce / dopravce (zákon o silniční dopravě)
    "sme_client_money_handler",  # osoba nakládající s prostředky klienta (AML / zákon č. 253/2008 Sb.)
    "sme_real_estate_operator",  # osoba nakládající s nemovitostmi (realitní / katastrální povinnosti)
    "sme_cross_border_trader",  # přeshraniční obchodník (Intrastat, přeshraniční DPH, vysílání pracovníků)
]

# The five BusinessProfile boolean compliance gates, in model field order (see storage/models.py).
# Named here so the mapping table uses the same keys as L0 intake.
GateFacts = Literal[
    "processes_personal_data",
    "handles_client_money",
    "has_real_estate",
    "cross_border",
    "is_employer",
]


@dataclass(frozen=True)
class SmeProfileFacts:
    """The BusinessProfile facts the role table reads.

    A plain value object (no database, no ORM), so the mapping is unit-testable in isolation and
    ``sme_role_dict_from_profile`` (below) is the only code that touches the ORM.
    """

    legal_form: str | None = (
        None  # sro, as, osvč, ks, vos, se, družstvo or other (BusinessProfile.legal_form)
    )
    nace_code: str | None = None  # for example "10.71" bakery, "49.41" road freight
    processes_personal_data: bool = False
    handles_client_money: bool = False
    has_real_estate: bool = False
    cross_border: bool = False
    is_employer: bool = False


# Legal form to base role. A substring match on the normalised legal_form (free text or enum), most
# specific first. Keys cover the BusinessProfile.legal_form vocabulary plus common Czech spellings.
_LEGAL_FORM_ROLES: tuple[tuple[str, SmeRole], ...] = (
    ("osvč", "sme_self_employed"),
    ("osvc", "sme_self_employed"),
    ("živnost", "sme_self_employed"),
    ("zivnost", "sme_self_employed"),
    ("s.r.o", "sme_limited_company"),
    ("sro", "sme_limited_company"),
    ("společnost s ručením", "sme_limited_company"),
    ("spolecnost s rucenim", "sme_limited_company"),
    ("a.s", "sme_joint_stock_company"),
    ("akciov", "sme_joint_stock_company"),
    ("družstv", "sme_cooperative"),
    ("druzstv", "sme_cooperative"),
    # "as" is deliberately not a bare key: it occurs inside many words ("has", "class"). Only the
    # dotted "a.s" and "akciov..." forms map here; the bare enum value "as" is handled by the exact
    # match below.
)

# Exact-match legal_form enum values, applied before the substring pass so the ambiguous bare "as",
# "se", "ks" and "vos" values resolve cleanly.
_LEGAL_FORM_EXACT: dict[str, SmeRole] = {
    "as": "sme_joint_stock_company",
    "se": "sme_joint_stock_company",  # societas europaea: treated like a.s. (ZOK thresholds)
    "ks": "sme_limited_company",  # komanditní společnost: a partnership; nearest bookkeeping peer
    "vos": "sme_limited_company",  # veřejná obchodní společnost: likewise
    "sro": "sme_limited_company",
    "osvč": "sme_self_employed",
    "osvc": "sme_self_employed",
    "družstvo": "sme_cooperative",
    "druzstvo": "sme_cooperative",
}

# The five gate booleans to additional roles. A gate that is True adds its role on top of the
# legal-form role (an s.r.o. that employs people and processes personal data holds
# sme_limited_company, sme_employer and sme_data_controller).
_GATE_ROLES: tuple[tuple[str, SmeRole], ...] = (
    ("is_employer", "sme_employer"),
    ("processes_personal_data", "sme_data_controller"),
    ("handles_client_money", "sme_client_money_handler"),
    ("has_real_estate", "sme_real_estate_operator"),
    ("cross_border", "sme_cross_border_trader"),
)

# NACE division (first two digits) to activity role. Only a small, high-confidence set of divisions
# where the activity itself creates a named addressee duty. Matched on the leading digits of
# nace_code ("10.71" is division "10", a food business). An unlisted division adds no role; the
# legal-form and gate roles still apply.
_NACE_PREFIX_ROLES: tuple[tuple[str, SmeRole], ...] = (
    ("10", "sme_food_business"),  # výroba potravinářských výrobků
    ("11", "sme_food_business"),  # výroba nápojů
    ("56", "sme_food_business"),  # stravování a pohostinství (restaurants/catering)
    (
        "47",
        "sme_food_business",
    ),  # coarse: all retail, including food retail; treated as food business pending review
    ("49", "sme_carrier"),  # pozemní a potrubní doprava (road freight/transport)
    ("50", "sme_carrier"),  # vodní doprava
    ("51", "sme_carrier"),  # letecká doprava
    ("52", "sme_carrier"),  # skladování a vedlejší činnosti v dopravě
)


def _norm(v: str | None) -> str:
    return (v or "").strip().lower()


def _legal_form_role(legal_form: str | None) -> SmeRole | None:
    """Map the legal_form to its base role: exact enum match first, then most specific substring."""
    raw = _norm(legal_form)
    if not raw:
        return None
    if raw in _LEGAL_FORM_EXACT:
        return _LEGAL_FORM_EXACT[raw]
    for needle, role in _LEGAL_FORM_ROLES:
        if needle in raw:
            return role
    return None


def _nace_role(nace_code: str | None) -> SmeRole | None:
    """Map a NACE code to an activity role by its two-digit division prefix."""
    raw = _norm(nace_code).replace(" ", "")
    if not raw:
        return None
    # "10.71", "1071" and "10" all normalise to the leading two digits
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 2:
        return None
    prefix = digits[:2]
    for pfx, role in _NACE_PREFIX_ROLES:
        if prefix == pfx:
            return role
    return None


def resolve_sme_roles(facts: SmeProfileFacts) -> list[SmeRole]:
    """Map profile facts to roles: the legal-form role, each True gate's role, then the NACE role.

    Deterministic and order-stable (base first, gates in field order, NACE last), de-duplicated in
    order. An empty profile yields ``[]``, which makes the role dict ``assumed`` so the gate keeps
    everything, just like the financial vocabulary's unknown default.
    """
    ordered: list[SmeRole] = []

    base = _legal_form_role(facts.legal_form)
    if base is not None:
        ordered.append(base)

    gate_values = {
        "processes_personal_data": facts.processes_personal_data,
        "handles_client_money": facts.handles_client_money,
        "has_real_estate": facts.has_real_estate,
        "cross_border": facts.cross_border,
        "is_employer": facts.is_employer,
    }
    for gate_key, role in _GATE_ROLES:
        if gate_values.get(gate_key):
            ordered.append(role)

    nace = _nace_role(facts.nace_code)
    if nace is not None:
        ordered.append(nace)

    # De-duplicate, keeping first-seen order.
    seen: set[SmeRole] = set()
    out: list[SmeRole] = []
    for r in ordered:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def sme_role_dict(facts: SmeProfileFacts) -> dict:
    """Build a role dict in the same shape as ``applicability.project_entity_role``.

    ``decide_binding``'s matrix and guard consume it unchanged. The primary role is the first
    resolved role; the full set is in ``sme_roles`` for render and reasons. ``assumed=True`` when
    no role resolved, so the guard keeps everything. ``excludes`` is always empty and
    ``has_top_up`` always True: these roles never set a financial exclusion, so the
    ``declared_exclusion`` out_of_scope path is unreachable (see the module docstring).
    """
    roles = resolve_sme_roles(facts)
    primary = roles[0] if roles else "unknown"
    return {
        "primary": primary,
        "sme_roles": roles,  # the full resolved set (for render); decide_binding ignores it
        "excludes": [],  # never a financial exclusion, so declared_exclusion is unreachable
        "has_top_up": True,  # unused here; kept for shape compatibility with project_entity_role
        "assumed": len(roles) == 0,  # no facts: the guard keeps everything
        "source": "sme_profile",
        "entity_role_version": 1,
        "sme_role_version": 1,
    }


def sme_role_dict_from_profile(profile) -> dict:
    """Build the role dict from a BusinessProfile, or any object with the same attributes.

    The only code in this module that touches the ORM. Duck-typed via ``getattr``, so a
    SimpleNamespace stand-in works in tests without a database. A None profile gives empty facts
    and an ``assumed`` role, so the gate keeps everything.
    """
    facts = SmeProfileFacts(
        legal_form=getattr(profile, "legal_form", None),
        nace_code=getattr(profile, "nace_code", None),
        processes_personal_data=bool(getattr(profile, "processes_personal_data", False)),
        handles_client_money=bool(getattr(profile, "handles_client_money", False)),
        has_real_estate=bool(getattr(profile, "has_real_estate", False)),
        cross_border=bool(getattr(profile, "cross_border", False)),
        is_employer=bool(getattr(profile, "is_employer", False)),
    )
    return sme_role_dict(facts)
