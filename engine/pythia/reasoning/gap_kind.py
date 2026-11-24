"""Vocabulary of ``gap_kind`` codes: why a practical answer could not be fully grounded.

A ``gap_kind`` names the specific missing material behind a "we can't tell you X" moment: no fact
row was extracted, the provision text does not say, the implementing decree is not in the corpus,
and so on. App-side gap telemetry uses the same enum so the operator can see, per query, what the
answer ran into.

Stability rules:
  * Import ``GapKind`` / ``GAP_KINDS`` from here instead of hard-coding strings, so the vocabulary
    has one source of truth.
  * The string values are the wire and telemetry form. Never rename or remove a value; add a new
    member instead.
  * Pure standard library with no I/O, so importing the module has no side effects.
"""

from __future__ import annotations

from enum import Enum


class GapKind(str, Enum):
    """Reason a practical fact or grounded answer is unavailable.

    The ``str`` mixin makes each member equal to its wire value, so
    ``GapKind.NO_FACT_ROW == "no_fact_row"`` and ``json.dumps`` writes the bare string.
    """

    # Gaps in the extracted practical facts (the practical_fact table).
    NO_FACT_ROW = "no_fact_row"  # the provision has no practical_fact row for the asked type
    NO_TEXT_SUPPORT = "no_text_support"  # a live regex over the provision's raw text also finds nothing
    # The extractor never processed the provision's act (it is absent from extraction_coverage).
    # This is a silent fallback, not a real fact gap: no_fact_row means the act was covered but this
    # provision has no fact. No gap event is emitted.
    EXTRACTION_NOT_RUN = "extraction_not_run"

    # Corpus coverage gaps: the instrument needed is not in the corpus.
    DECREE_ABSENT = "decree_absent"  # the implementing vyhláška or nařízení is not ingested
    # The registry that resolves which office is competent is not loaded.
    AUTHORITY_REGISTRY_ABSENT = "authority_registry_absent"
    SOFT_LAW_ABSENT = "soft_law_absent"  # the guidance (metodika, sdělení) holding the answer is absent
    CASE_LAW_ABSENT = "case_law_absent"  # no apex or on-point lower-court ruling covers the point

    # Practical-slot locator gaps: the corpus names the body or rule but not the concrete pointer.
    # A `where` slot: the corpus names the type of authority but not the local office to walk into.
    AUTHORITY_LOCATOR = "authority_locator"
    # A `steps` slot: the online form, portal or datová schránka channel is not in the statute text.
    FORM_PORTAL = "form_portal"
    # An annual instrument (minimum-wage government regulation, sdělení) is past its reissue window,
    # so the value in the corpus may be superseded.
    ANNUAL_INSTRUMENT_STALE = "annual_instrument_stale"

    # Structural and resolution gaps.
    PROVISION_NOT_FOUND = "provision_not_found"  # the cited provision id resolves to nothing
    AMBIGUOUS_REFERENCE = "ambiguous_reference"  # matches several candidates; cannot pick one safely
    OUT_OF_SCOPE = "out_of_scope"  # the question is outside the domains the corpus covers

    # Catch-all. Keep it last and use it only when no specific kind fits.
    UNKNOWN = "unknown"


# All wire values in declaration order (telemetry and allow-lists iterate this).
GAP_KINDS: tuple[str, ...] = tuple(k.value for k in GapKind)


# The eight backlog codes: the subset of GapKind that gap_kind_derive.py assigns to demoted claims
# and gap_backlog.py ranks, feeding the queue of decrees and connectors to add. The app mirrors
# these exact strings and a test on each side fails if they drift apart. The order is stable
# (gap_backlog uses it as a tie-break). Structural, resolution and catch-all kinds are not backlog
# codes because no connector can fix them.
GAP_KIND_BACKLOG_CODES: tuple[str, ...] = (
    GapKind.NO_FACT_ROW.value,
    GapKind.NO_TEXT_SUPPORT.value,
    GapKind.DECREE_ABSENT.value,
    GapKind.SOFT_LAW_ABSENT.value,
    GapKind.AUTHORITY_LOCATOR.value,
    GapKind.FORM_PORTAL.value,
    GapKind.ANNUAL_INSTRUMENT_STALE.value,
    GapKind.EXTRACTION_NOT_RUN.value,
)


def is_gap_kind(value: str) -> bool:
    """Return True if ``value`` is a known gap_kind wire string. Never raises."""
    return value in _VALUE_SET


def is_backlog_code(value: str) -> bool:
    """Return True if ``value`` is one of the eight backlog codes. Never raises."""
    return value in _BACKLOG_SET


_VALUE_SET = frozenset(GAP_KINDS)
_BACKLOG_SET = frozenset(GAP_KIND_BACKLOG_CODES)
