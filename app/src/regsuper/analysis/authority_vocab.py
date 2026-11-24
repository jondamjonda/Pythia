"""The shared vocabulary of Czech competent-authority types.

One canonical list of authority types, so that every consumer uses the same tag:

* the practical-facts 'authority' extractor, which turns § text such as
  "finanční úřad" or "OSSZ" into an abstract authority-type tag;
* the authority registry (``authority.authority_type``, migration 022);
* the answer-time directory join (in api/answer.py, not here) that renders
  "Váš úřad: FÚ pro Jihomoravský kraj...".

A tag is a short, stable slug (``financni_urad``). Each tag carries its Czech
label and the surface aliases seen in § text, so an extractor can map free text
to a tag with ``classify_authority_text`` and the registry loader can validate a
raw ``authority_type`` cell with ``normalize_tag``.

Pure and dependency-free (no database, no LLM), so it is safe to import from
ingest, analysis and tests alike.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthorityType:
    """One competent-authority type in the shared vocabulary.

    Attributes:
        tag: Stable slug, the value stored in ``authority.authority_type``.
        label: Czech display label.
        aliases: Surface forms seen in § text or directories that map to this tag,
            matched as whole words, ignoring case and diacritics.
    """

    tag: str
    label: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


# The canonical vocabulary. These are the same tags the practical-facts
# 'authority' extractor emits; keep the two in lockstep. Order does not affect
# classification: the longest matching alias always wins.
AUTHORITY_TYPES: tuple[AuthorityType, ...] = (
    AuthorityType(
        tag="financni_urad",
        label="Finanční úřad / Územní pracoviště",
        aliases=(
            "finanční úřad",
            "finančního úřadu",
            "finančnímu úřadu",
            "územní pracoviště",
            "územního pracoviště",
            "správce daně",
            "finanční správa",
            "FÚ",
            "ÚzP",
        ),
    ),
    AuthorityType(
        tag="ossz",
        label="Okresní správa sociálního zabezpečení (OSSZ/ČSSZ)",
        aliases=(
            "okresní správa sociálního zabezpečení",
            "okresní správu sociálního zabezpečení",
            "správa sociálního zabezpečení",
            "OSSZ",
            "PSSZ",  # Pražská SSZ
            "MSSZ",  # Městská SSZ Brno
            "ČSSZ",
            "Česká správa sociálního zabezpečení",
        ),
    ),
    AuthorityType(
        tag="khs",
        label="Krajská hygienická stanice (KHS)",
        aliases=(
            "krajská hygienická stanice",
            "krajské hygienické stanice",
            "krajskou hygienickou stanici",
            "hygienická stanice",
            "orgán ochrany veřejného zdraví",
            "KHS",
        ),
    ),
    AuthorityType(
        tag="oip",
        label="Oblastní inspektorát práce (OIP / SÚIP)",
        aliases=(
            "oblastní inspektorát práce",
            "oblastního inspektorátu práce",
            "oblastnímu inspektorátu práce",
            "inspektorát práce",
            "inspektorátu práce",
            "inspekce práce",
            "Státní úřad inspekce práce",
            "OIP",
            "SÚIP",
        ),
    ),
    AuthorityType(
        tag="zivnostensky_urad",
        label="Živnostenský úřad",
        aliases=(
            "živnostenský úřad",
            "živnostenského úřadu",
            "obecní živnostenský úřad",
            "živnostenský odbor",
            "živnostenský rejstřík",
        ),
    ),
    AuthorityType(
        tag="urad_prace",
        label="Úřad práce ČR",
        aliases=(
            "úřad práce",
            "úřadu práce",
            "Úřad práce ČR",
            "krajská pobočka úřadu práce",
        ),
    ),
    AuthorityType(
        tag="katastralni_urad",
        label="Katastrální úřad",
        aliases=(
            "katastrální úřad",
            "katastrálního úřadu",
            "katastrální pracoviště",
        ),
    ),
    AuthorityType(
        tag="stavebni_urad",
        label="Stavební úřad",
        aliases=(
            "stavební úřad",
            "stavebního úřadu",
            "obecný stavební úřad",
        ),
    ),
    AuthorityType(
        tag="celni_urad",
        label="Celní úřad",
        aliases=(
            "celní úřad",
            "celního úřadu",
            "Celní správa",
        ),
    ),
    AuthorityType(
        tag="uoou",
        label="Úřad pro ochranu osobních údajů (ÚOOÚ)",
        aliases=(
            "úřad pro ochranu osobních údajů",
            "úřadu pro ochranu osobních údajů",
            "dozorový úřad",
            "ÚOOÚ",
        ),
    ),
)


# Tag-to-AuthorityType lookup and the set of valid tags, built once.
_BY_TAG: dict[str, AuthorityType] = {t.tag: t for t in AUTHORITY_TYPES}
VALID_TAGS: frozenset[str] = frozenset(_BY_TAG)


def _fold(text: str) -> str:
    """Lower-case and strip diacritics for accent-insensitive matching."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower()


# Pre-fold every alias once: (folded_alias, tag), longest alias first so the
# most specific surface form wins when several match.
_ALIAS_INDEX: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((_fold(alias), t.tag) for t in AUTHORITY_TYPES for alias in t.aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def get(tag: str) -> AuthorityType | None:
    """Return the AuthorityType for a tag, or None if unknown."""
    return _BY_TAG.get(tag)


def is_valid_tag(tag: str | None) -> bool:
    """Return True if ``tag`` is a known authority-type tag."""
    return tag in VALID_TAGS


def normalize_tag(raw: str | None) -> str | None:
    """Normalise a raw ``authority_type`` cell to a canonical tag, or None.

    Accepts either an already-canonical tag (returned as is) or a Czech alias
    surface form (mapped through the alias index). Used by the registry loader
    so a directory CSV can carry human-readable names and still produce a
    canonical tag. Unknown input returns None; the loader decides whether to
    skip or keep the row.
    """
    if not raw:
        return None
    candidate = raw.strip()
    if candidate in VALID_TAGS:
        return candidate
    folded = _fold(candidate)
    if folded in VALID_TAGS:
        return folded
    for alias_folded, tag in _ALIAS_INDEX:
        if alias_folded == folded:
            return tag
    return None


def classify_authority_text(text: str | None) -> str | None:
    """Map free § text to an authority-type tag using the longest matching alias.

    Used by the practical-facts extractor: given a phrase or sentence, return
    the tag of the most specific authority alias it contains, else None. The
    registry stores the same tags, so extracted text joins directly to it.
    """
    if not text:
        return None
    folded = _fold(text)
    for alias_folded, tag in _ALIAS_INDEX:
        # Whole-word match, not a raw substring: a short alias like "FÚ" (folded "fu")
        # must not match inside "fungovat" or "funkce".
        if re.search(rf"\b{re.escape(alias_folded)}\b", folded):
            return tag
    return None
