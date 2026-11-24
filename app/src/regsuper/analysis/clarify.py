"""Deterministic pre-answer clarification for /api/answer, run before quota or engine calls.

When the saved business profile and the question do not pin down the facts that matter for the
detected type of matter, the honest and cheap move is to ask one bounded round of clarifying
questions before reserving quota or calling the reasoning engine. This module makes that decision.
It is pure (no database, I/O, LLM or engine call) and deterministic: it classifies the matter with
a small keyword lexicon, resolves each required slot from resubmitted typed slots, the profile's
AnswerContext or a regex read of the query, and returns at most three fixed questions for any
slot still missing.

The lexicon (``MATTER_LEXICON``), the required-slot matrix (``MATTER_REQUIRED_SLOTS``) and the
question copy (``CLARIFY_CATALOG``) are drafts awaiting lawyer review before the
``answer_clarify_enabled`` flag is turned on. The LLM never writes a clarifying question; the copy
is a fixed table. A second, engine-side clarification driven by how well the corpus covers the
matter is not part of this module.

Public API:

* ``detect_matter(query, answer_context)`` returns a matter-type string.
* ``resolve_slots(query, answer_context, slots_provided)`` returns a ``SlotResolution`` with the
  resolved slot values and the ordered list of required slots still missing.
* ``clarify_questions(missing_required, lang)`` returns up to three ``ClarifyQuestion`` from the
  fixed catalog.
* ``SLOT_ENUMS`` lists the allowed values per slot, so the API layer can validate a resubmitted
  ``slots`` dict (an unknown key or value is rejected).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary: matter types and slot keys (draft, pending lawyer review).
# ---------------------------------------------------------------------------
# The matter types this module recognises. `general` is the fallback and has no required
# slots, so a query classified as `general` never triggers clarification: an unrecognised
# matter must not block the answer, it goes straight to the engine.
MATTER_EMPLOYMENT = "employment"
MATTER_VAT = "vat"
MATTER_LICENSING = "licensing"
MATTER_DATA_PROTECTION = "data_protection"
MATTER_GENERAL = "general"

MATTER_TYPES: tuple[str, ...] = (
    MATTER_EMPLOYMENT,
    MATTER_VAT,
    MATTER_LICENSING,
    MATTER_DATA_PROTECTION,
    MATTER_GENERAL,
)

# The slot keys that can be required and resolved. Each is a single decision-relevant fact.
SLOT_LEGAL_FORM = "legal_form"
SLOT_IS_EMPLOYER = "is_employer"
SLOT_HEADCOUNT_BAND = "headcount_band"
SLOT_PROCESSES_PERSONAL_DATA = "processes_personal_data"
SLOT_CROSS_BORDER = "cross_border"
SLOT_HANDLES_CLIENT_MONEY = "handles_client_money"
SLOT_HAS_REAL_ESTATE = "has_real_estate"
SLOT_REGION = "region"  # the kraj / municipality the business sits in

# The allowed values per slot, which the API layer validates a resubmitted `slots` dict
# against. A boolean gate slot takes "yes" or "no"; enumerated slots take their profile
# vocabulary values; region is validated only as a non-empty string, not enumerated.
_BOOL_SLOT_VALUES: tuple[str, ...] = ("yes", "no")
SLOT_ENUMS: dict[str, tuple[str, ...]] = {
    SLOT_LEGAL_FORM: ("sro", "as", "osvc", "ks", "vos", "se", "druzstvo", "other"),
    SLOT_IS_EMPLOYER: _BOOL_SLOT_VALUES,
    SLOT_HEADCOUNT_BAND: ("1", "2-9", "10-49", "50-249", "250+"),
    SLOT_PROCESSES_PERSONAL_DATA: _BOOL_SLOT_VALUES,
    SLOT_CROSS_BORDER: _BOOL_SLOT_VALUES,
    SLOT_HANDLES_CLIENT_MONEY: _BOOL_SLOT_VALUES,
    SLOT_HAS_REAL_ESTATE: _BOOL_SLOT_VALUES,
    # region is not a fixed enum (thousands of municipalities, 14 kraje); the API layer
    # validates it as a non-empty string. Absence from SLOT_ENUMS means "free string".
}

# ---------------------------------------------------------------------------
# Matter-classification lexicon (draft): keyword to matter, no LLM.
# ---------------------------------------------------------------------------
# Small, explicit Czech (and a few English) keyword lists, matched case-insensitively as
# substrings of the lower-cased query. The first matter in MATTER_TYPES order (skipping
# `general`) with a hit wins, so the order encodes priority. Kept deliberately small;
# broadening it needs lawyer review.
MATTER_LEXICON: dict[str, tuple[str, ...]] = {
    MATTER_EMPLOYMENT: (
        "zaměstna",
        "zamestna",
        "mzd",
        "mzda",
        "výpověď",
        "vypoved",
        "pracovní smlouv",
        "pracovni smlouv",
        "zákoník práce",
        "zakonik prace",
        "dovolen",
        "employee",
        "employment",
        "payroll",
        "wage",
    ),
    MATTER_VAT: (
        "dph",
        "daň z přidané",
        "dan z pridane",
        "vat",
        "plátce dph",
        "platce dph",
        "kontrolní hlášení",
        "kontrolni hlaseni",
        "daňové přiznání",
        "danove priznani",
    ),
    MATTER_LICENSING: (
        "živnost",
        "zivnost",
        "licenc",
        "koncese",
        "povolení k podnikání",
        "povoleni k podnikani",
        "oprávnění",
        "opravneni",
        "licence",
        "permit",
        "trade licen",
    ),
    MATTER_DATA_PROTECTION: (
        "gdpr",
        "osobní údaj",
        "osobni udaj",
        "ochrana údajů",
        "ochrana udaju",
        "zpracování údajů",
        "zpracovani udaju",
        "personal data",
        "data protection",
        "subjekt údajů",
    ),
}

# ---------------------------------------------------------------------------
# Required-slot matrix (draft): matter to ordered required slot keys.
# ---------------------------------------------------------------------------
# The decision-relevant facts a matter needs before an answer is worth the spend. Ordered:
# the first three missing required slots become the clarifying questions. `general` has no
# required slots and never clarifies. Small and explicit on purpose, so a lawyer can review it.
MATTER_REQUIRED_SLOTS: dict[str, tuple[str, ...]] = {
    MATTER_EMPLOYMENT: (SLOT_IS_EMPLOYER, SLOT_HEADCOUNT_BAND, SLOT_LEGAL_FORM),
    MATTER_VAT: (SLOT_LEGAL_FORM, SLOT_CROSS_BORDER),
    MATTER_LICENSING: (SLOT_LEGAL_FORM, SLOT_REGION),
    MATTER_DATA_PROTECTION: (SLOT_PROCESSES_PERSONAL_DATA, SLOT_CROSS_BORDER),
    MATTER_GENERAL: (),
}

# ---------------------------------------------------------------------------
# Clarifying-question catalog: fixed Czech and English draft copy per slot. The LLM
# never writes these. Keyed by slot; at most three are picked per turn.
# ---------------------------------------------------------------------------
CLARIFY_CATALOG: dict[str, dict[str, str]] = {
    SLOT_LEGAL_FORM: {
        "cs": "V jaké právní formě podnikáte (např. s.r.o., a.s., OSVČ)?",
        "en": "What is your legal form (e.g. s.r.o., a.s., sole trader)?",
    },
    SLOT_IS_EMPLOYER: {
        "cs": "Zaměstnáváte zaměstnance (máte alespoň jednoho zaměstnance)?",
        "en": "Do you employ staff (at least one employee)?",
    },
    SLOT_HEADCOUNT_BAND: {
        "cs": "Kolik máte zaměstnanců (přibližné pásmo: 1, 2–9, 10–49, 50–249, 250+)?",
        "en": "How many employees do you have (band: 1, 2–9, 10–49, 50–249, 250+)?",
    },
    SLOT_PROCESSES_PERSONAL_DATA: {
        "cs": "Zpracováváte osobní údaje (např. zákazníků nebo zaměstnanců)?",
        "en": "Do you process personal data (e.g. of customers or employees)?",
    },
    SLOT_CROSS_BORDER: {
        "cs": "Působíte přeshraničně (obchodujete nebo poskytujete služby mimo ČR)?",
        "en": "Do you operate cross-border (trade or provide services outside the CR)?",
    },
    SLOT_HANDLES_CLIENT_MONEY: {
        "cs": "Nakládáte s peněžními prostředky klientů?",
        "en": "Do you handle client money?",
    },
    SLOT_HAS_REAL_ESTATE: {
        "cs": "Vlastníte nebo užíváte nemovitost pro podnikání?",
        "en": "Do you own or use real estate for the business?",
    },
    SLOT_REGION: {
        "cs": "Ve kterém kraji nebo obci má firma sídlo?",
        "en": "In which region or municipality is the business established?",
    },
}

# Maximum clarifying questions per round (there is only ever one bounded round).
MAX_CLARIFY_QUESTIONS = 3

_DEFAULT_LANG = "cs"
_SUPPORTED_LANGS = ("cs", "en")

# ---------------------------------------------------------------------------
# Deterministic query readers (regex, no LLM).
# ---------------------------------------------------------------------------
# Legal form mentioned in the query text, mapped to the profile vocabulary. Case-insensitive.
_LEGAL_FORM_QUERY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bs\.?\s*r\.?\s*o\.?\b|\bs r o\b|společnost s ručením|spolecnost s rucenim", "sro"),
    (r"\ba\.?\s*s\.?\b|akciová společnost|akciova spolecnost", "as"),
    (r"\bosvč\b|\bosvc\b|živnostník|zivnostnik|sole trader|self.?employed", "osvc"),
    (r"\bv\.?\s*o\.?\s*s\.?\b|veřejná obchodní|verejna obchodni", "vos"),
    (r"\bk\.?\s*s\.?\b|komanditní|komanditni", "ks"),
    (r"družstv|druzstv|cooperative", "druzstvo"),
)

# Gate booleans the query can affirm. A gate is resolved from the query only on a clear
# affirmative mention, which sets it to "yes". Absence is not a signal: the gate stays
# unresolved. We never infer "no" from silence.
_GATE_QUERY_AFFIRM: dict[str, tuple[str, ...]] = {
    SLOT_IS_EMPLOYER: (
        "zaměstnávám",
        "zamestnavam",
        "máme zaměstnance",
        "mame zamestnance",
        "naši zaměstnanci",
        "nasi zamestnanci",
        "we employ",
        "our employees",
    ),
    SLOT_PROCESSES_PERSONAL_DATA: (
        "zpracováváme osobní",
        "zpracovavame osobni",
        "sbíráme osobní",
        "sbirame osobni",
        "we process personal",
    ),
    SLOT_CROSS_BORDER: (
        "přeshranič",
        "preshranic",
        "do zahraničí",
        "do zahranici",
        "mimo čr",
        "mimo cr",
        "cross-border",
        "cross border",
        "export",
        "import",
    ),
    SLOT_HANDLES_CLIENT_MONEY: (
        "peníze klientů",
        "penize klientu",
        "prostředky klientů",
        "prostredky klientu",
        "client money",
    ),
    SLOT_HAS_REAL_ESTATE: ("nemovitost", "budova", "pozemek", "real estate", "premises"),
}


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SlotResolution:
    """Slot resolution for a matter.

    Holds the matter type, the resolved slot values (keyed by slot) and the ordered list of
    required slots that are still missing.
    """

    matter_type: str
    resolved: dict[str, Any] = field(default_factory=dict)
    missing_required: list[str] = field(default_factory=list)

    @property
    def needs_clarify(self) -> bool:
        """Return True if at least one required slot is missing, so a clarify round is due."""
        return bool(self.missing_required)


@dataclass(frozen=True)
class ClarifyQuestion:
    """One fixed clarifying question: the slot it fills and its copy in the chosen language."""

    slot: str
    text: str


# ---------------------------------------------------------------------------
# Matter classification: deterministic keyword scan of the query.
# ---------------------------------------------------------------------------
def detect_matter(query: str, answer_context: Any | None = None) -> str:
    """Classify the matter deterministically from a keyword scan of ``query``.

    The first matter (in MATTER_TYPES priority order) whose lexicon matches a substring of the
    lower-cased query wins. No match gives ``general``, which has no required slots and so
    never clarifies. ``answer_context`` is accepted for a future profile-derived hint but is
    currently unused: the profile fills a matter's slots, it does not choose the matter.
    """
    q = (query or "").lower()
    for matter in MATTER_TYPES:
        if matter == MATTER_GENERAL:
            continue
        if any(kw in q for kw in MATTER_LEXICON.get(matter, ())):
            return matter
    return MATTER_GENERAL


# ---------------------------------------------------------------------------
# Slot resolution: typed slots first, then the profile, then a read of the query.
# ---------------------------------------------------------------------------
def _bool_to_slot(value: Any) -> str | None:
    """Map a Python bool to the 'yes'/'no' slot vocabulary (anything else gives None)."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return None


def _resolve_legal_form_from_query(query: str) -> str | None:
    q = (query or "").lower()
    for pattern, value in _LEGAL_FORM_QUERY_PATTERNS:
        if re.search(pattern, q):
            return value
    return None


def _resolve_gate_from_query(slot: str, query: str) -> str | None:
    """Return 'yes' on a clear affirmative gate mention in the query, else None (never 'no')."""
    q = (query or "").lower()
    for kw in _GATE_QUERY_AFFIRM.get(slot, ()):
        if kw in q:
            return "yes"
    return None


def _ctx_get(answer_context: Any, attr: str) -> Any:
    if answer_context is None:
        return None
    return getattr(answer_context, attr, None)


def _resolve_one_slot(
    slot: str,
    *,
    query: str,
    answer_context: Any,
    slots_provided: dict[str, Any] | None,
) -> Any:
    """Resolve a single slot, or return None if it cannot be resolved.

    Sources in precedence order:

    1. the typed slot the caller resubmitted (an explicit answer, so it wins);
    2. the profile's AnswerContext;
    3. a deterministic regex read of the query (legal form and gate booleans only).
    """
    # 1) Resubmitted typed slot.
    if slots_provided:
        v = slots_provided.get(slot)
        if v not in (None, ""):
            return v

    # 2) Profile / AnswerContext.
    if slot == SLOT_LEGAL_FORM:
        ctx_v = _ctx_get(answer_context, "legal_form")
        if ctx_v:
            return str(ctx_v)
    elif slot == SLOT_HEADCOUNT_BAND:
        ctx_v = _ctx_get(answer_context, "headcount_band")
        if ctx_v:
            return str(ctx_v)
    elif slot == SLOT_REGION:
        # A validated municipality_code on the profile pins the business location, which
        # satisfies the region slot without reading the query or the database.
        ctx_v = _ctx_get(answer_context, "municipality_code")
        if ctx_v:
            return str(ctx_v)
    elif slot in _GATE_QUERY_AFFIRM or slot == SLOT_IS_EMPLOYER:
        # One of the profile's five gate booleans (is_employer included).
        ctx_v = _bool_to_slot(_ctx_get(answer_context, slot))
        # AnswerContext defaults every gate to False, so a profile that exists but never set
        # the gate reads "no". That counts as an explicit profile answer (the profile owns
        # these gates). Only a missing context (None) leaves the gate for the query to decide.
        if answer_context is not None and ctx_v is not None:
            return ctx_v

    # 3) Deterministic query read.
    if slot == SLOT_LEGAL_FORM:
        qv = _resolve_legal_form_from_query(query)
        if qv:
            return qv
    elif slot in _GATE_QUERY_AFFIRM or slot == SLOT_IS_EMPLOYER:
        qv = _resolve_gate_from_query(slot, query)
        if qv:
            return qv

    return None


def resolve_slots(
    query: str,
    answer_context: Any | None = None,
    slots_provided: dict[str, Any] | None = None,
) -> SlotResolution:
    """Resolve the required slots for the matter detected in ``query``.

    Each required slot is resolved from the resubmitted typed slots, then the profile's
    AnswerContext, then a deterministic read of the query. Returns the resolved values and the
    ordered list of required slots still missing. No database, I/O or LLM.
    """
    matter = detect_matter(query, answer_context)
    required = MATTER_REQUIRED_SLOTS.get(matter, ())

    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for slot in required:
        value = _resolve_one_slot(
            slot,
            query=query,
            answer_context=answer_context,
            slots_provided=slots_provided,
        )
        if value in (None, ""):
            missing.append(slot)
        else:
            resolved[slot] = value

    return SlotResolution(matter_type=matter, resolved=resolved, missing_required=missing)


# ---------------------------------------------------------------------------
# Question selection: up to three fixed questions for the missing required slots.
# ---------------------------------------------------------------------------
def clarify_questions(
    missing_required: list[str], lang: str = _DEFAULT_LANG
) -> list[ClarifyQuestion]:
    """Pick up to MAX_CLARIFY_QUESTIONS fixed questions for the missing required slots.

    Slots are taken in the given order. A slot with no catalog entry is skipped, so copy is
    never invented. This is a table lookup; no LLM writes the questions.
    """
    chosen: str = lang if lang in _SUPPORTED_LANGS else _DEFAULT_LANG
    out: list[ClarifyQuestion] = []
    for slot in missing_required:
        if len(out) >= MAX_CLARIFY_QUESTIONS:
            break
        entry = CLARIFY_CATALOG.get(slot)
        if not entry:
            continue
        out.append(ClarifyQuestion(slot=slot, text=entry.get(chosen) or entry[_DEFAULT_LANG]))
    return out


def validate_slots(slots_provided: dict[str, Any]) -> str | None:
    """Structurally validate a resubmitted ``slots`` dict against SLOT_ENUMS.

    Returns an error message if any key is not a known slot or any enumerated slot's value is
    outside its allowed set (region is validated only as a non-empty string). Returns None
    when the dict is valid. The API layer uses this to reject a malformed follow-up with 422
    while keeping ``extra='forbid'`` on the request model.
    """
    for key, value in slots_provided.items():
        if key not in CLARIFY_CATALOG:
            return f"unknown slot: {key!r}"
        allowed = SLOT_ENUMS.get(key)
        if allowed is None:
            # Free-string slot (region): require a non-empty string.
            if not isinstance(value, str) or not value.strip():
                return f"invalid value for slot {key!r}"
        else:
            if value not in allowed:
                return f"invalid value for slot {key!r}: {value!r}"
    return None


__all__ = [
    "CLARIFY_CATALOG",
    "MATTER_LEXICON",
    "MATTER_REQUIRED_SLOTS",
    "MATTER_TYPES",
    "MAX_CLARIFY_QUESTIONS",
    "SLOT_ENUMS",
    "ClarifyQuestion",
    "SlotResolution",
    "clarify_questions",
    "detect_matter",
    "resolve_slots",
    "validate_slots",
]
