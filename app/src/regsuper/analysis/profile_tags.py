"""Tag derivation for small-business profiles, the counterpart of L0 intake.

Small and medium businesses are described by a ``BusinessProfile`` row (legal form, NACE code, the
five compliance gates) rather than the ``ProjectIntake`` form. This module maps a profile onto the
same controlled tag vocabularies the financial lane uses (``SECTOR_TO_TAGS`` and
``ENTITY_TO_ACTIVITY`` in l0_intake), so both lanes share one taxonomy and one set of evaluation
invariants.

A pure, deterministic helper: no LLM, database or I/O. It does not change
``l0_intake.derive_tags`` or ``create_project``; it only imports their vocabulary maps and the
``_norm`` helper, so the vocabulary is never forked. Like ``derive_tags`` it passes unknown values
through and de-duplicates while preserving order.

Mapping summary
---------------
sector_tags come from (in order, before dedup):
  * any explicit `sector_override` (run through SECTOR_TO_TAGS, else raw);
  * the NACE-code section letter (the first alpha char of `nace_code`)
    mapped to a coarse sector via `_NACE_SECTION_TO_SECTOR`, then expanded
    through SECTOR_TO_TAGS;
  * gate-driven sector tags (`has_real_estate` gives real_estate, etc.);
  * any explicit `derived_sector_tags` already on the profile (merged last,
    so tags written by an operator or by L0 are kept without being placed
    ahead of the freshly derived ones).

activity_tags come from (in order, before dedup):
  * the `legal_form`, mapped to corporate or entity activity tags
    (`_LEGAL_FORM_TO_ACTIVITY`);
  * the five compliance gates, mapped to activity tags (`_GATE_TO_ACTIVITY`);
  * `regulated_activity` + each `additional_activities` entry run through
    ENTITY_TO_ACTIVITY (unknown values pass through as the raw normalised
    token, exactly like `derive_tags` does for `entity_type`);
  * any explicit `derived_activity_tags` already on the profile (merged
    last, same rationale as above).

Unknown `legal_form` / NACE section / activity values are never dropped:
they pass through as their normalised token so a later tagging pass on the
corpus can still match them.
"""

from __future__ import annotations

from typing import Any

from regsuper.analysis.l0_intake import (
    ENTITY_TO_ACTIVITY,
    SECTOR_TO_TAGS,
    _norm,
)

# ---------------------------------------------------------------------------
# Profile-specific deterministic mappings
# ---------------------------------------------------------------------------
# Legal form (sro, as, osvč, ks, vos, se, družstvo, other) to activity tags. Every
# Czech legal form is, first, a commercial/corporate entity; `osvč` (sole
# trader) additionally carries the self-employment angle. These slot into the
# same activity vocabulary the financial lane emits, so they widen the
# app-side activity filter rather than introducing a parallel taxonomy.
_LEGAL_FORM_TO_ACTIVITY: dict[str, list[str]] = {
    "sro": ["commercial", "corporate"],
    "as": ["commercial", "corporate"],
    "se": ["commercial", "corporate"],
    "ks": ["commercial", "corporate"],
    "vos": ["commercial", "corporate"],
    "družstvo": ["commercial", "corporate"],
    "osvč": ["self_employed", "commercial"],
}

# The five compliance gates (BusinessProfile booleans) to controlled tags.
# Each gate is split into the sector tag(s) and activity tag(s) it implies so
# the two output channels stay clean. A gate that has no natural sector tag
# leaves `_GATE_TO_SECTOR` empty for that key.
_GATE_TO_ACTIVITY: dict[str, list[str]] = {
    "processes_personal_data": ["data_protection"],
    "handles_client_money": ["aml", "client_funds"],
    "has_real_estate": ["real_property"],
    "cross_border": ["passporting"],
    "is_employer": ["employment", "occupational_safety"],
}
_GATE_TO_SECTOR: dict[str, list[str]] = {
    "has_real_estate": ["real_estate"],
}

# NACE section letter (the leading letter of a NACE code, so "C" for "C25") to
# a coarse sector key that exists in SECTOR_TO_TAGS, so the profile inherits
# the same expanded sector_tags an L0 intake of that sector would. Sections
# with no corpus sector are deliberately omitted (they fall through to the raw
# section token, never a phantom sector).
_NACE_SECTION_TO_SECTOR: dict[str, str] = {
    "C": "manufacturing",
    "D": "energy",
    "F": "construction",
    "G": "retail",
    "H": "transport",
    "K": "fin_services",
    "L": "real_estate",
    "Q": "healthcare",
}


def _expand_sector(value: str) -> list[str]:
    """Expand a normalised sector key through SECTOR_TO_TAGS (unknown keys pass through)."""
    return list(SECTOR_TO_TAGS.get(value, [value])) if value else []


def _dedup(tags: list[str]) -> list[str]:
    """De-duplicate while preserving order, as derive_tags does."""
    seen: set[str] = set()
    return [t for t in tags if not (t in seen or seen.add(t))]


def derive_tags_for_profile(profile: Any) -> tuple[list[str], list[str]]:
    """Map a BusinessProfile to ``(sector_tags, activity_tags)`` deterministically.

    `profile` may be a `BusinessProfile` ORM instance or any duck-typed object
    exposing the same attributes (legal_form, nace_code, sector_override,
    additional_activities, the five gate booleans, regulated_activity,
    derived_sector_tags, derived_activity_tags). Missing attributes default to
    the empty or false value, so partially populated stubs are safe.

    Reuses l0_intake's controlled vocabulary (SECTOR_TO_TAGS, ENTITY_TO_ACTIVITY)
    and its `_norm`. Unknown values pass through as their normalised token, and
    the result is de-duplicated in order, exactly like `derive_tags`.
    """
    # --- sector_tags --------------------------------------------------------
    sector_tags: list[str] = []

    override = _norm(getattr(profile, "sector_override", None))
    if override:
        sector_tags.extend(_expand_sector(override))

    nace_code = _norm(getattr(profile, "nace_code", None))
    if nace_code:
        # NACE section letters are conventionally upper-case ("C" for "C25");
        # `_norm` lower-cased the code, so upper the leading char for the lookup.
        section = nace_code[0].upper()
        mapped = _NACE_SECTION_TO_SECTOR.get(section)
        if mapped is not None:
            sector_tags.extend(_expand_sector(mapped))
        else:
            # Unknown NACE section: keep the section letter as a token rather
            # than dropping the signal entirely.
            sector_tags.append(section)

    for gate, tags in _GATE_TO_SECTOR.items():
        if getattr(profile, gate, False):
            sector_tags.extend(tags)

    # Merge any explicit sector tags already on the profile (written by L0 or
    # an operator), appended last so they don't reorder the derived ones and
    # de-duplicated away if redundant.
    sector_tags.extend(getattr(profile, "derived_sector_tags", None) or [])

    # --- activity_tags ------------------------------------------------------
    activity_tags: list[str] = []

    legal_form = _norm(getattr(profile, "legal_form", None))
    if legal_form:
        mapped_lf = _LEGAL_FORM_TO_ACTIVITY.get(legal_form)
        if mapped_lf is not None:
            activity_tags.extend(mapped_lf)
        else:
            # Unknown legal form (including the controlled "other"): pass through.
            activity_tags.append(legal_form)

    for gate, tags in _GATE_TO_ACTIVITY.items():
        if getattr(profile, gate, False):
            activity_tags.extend(tags)

    # regulated_activity and additional_activities go through the same
    # entity-to-activity vocabulary the financial lane uses; unknown values pass
    # through as their normalised token (as derive_tags does for entity_type).
    raw_activities: list[str] = []
    reg = getattr(profile, "regulated_activity", None)
    if reg:
        raw_activities.append(reg)
    raw_activities.extend(getattr(profile, "additional_activities", None) or [])
    for raw in raw_activities:
        key = _norm(raw)
        if not key:
            continue
        mapped_act = ENTITY_TO_ACTIVITY.get(key)
        if mapped_act is not None:
            activity_tags.extend(mapped_act)
        else:
            activity_tags.append(key)

    activity_tags.extend(getattr(profile, "derived_activity_tags", None) or [])

    return _dedup(sector_tags), _dedup(activity_tags)
