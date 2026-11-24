"""Temporal freshness: was the cited law in force on the analysis date?

A faithful restatement of a provision is only useful if the provision is still the law on the date
we are advising about. A finding can cite a provision that was already repealed, one not yet in
force, or one whose validity window has closed (``valid_to`` in the past), and the cascade itself
never asks whether the provision was in force on the analysis date.

Given a provision's temporal columns (``valid_from``, ``valid_to``, ``temporal_status``) and an
explicit ``analysis_as_of`` date, this module returns a freshness verdict that annotates a finding.
It makes no LLM call, database read, network call or clock read; the as-of date is passed in, so
the same inputs always give the same verdict and a historical analysis is reproducible. Render uses
it when ``temporal_freshness_enabled`` is on.

The verdict is a signal, never a filter: nothing here removes, suppresses or demotes a finding.
``temporal_freshness`` only returns a label, a reason and the relevant boundary date; render decides
how to show a ``superseded``, ``future_effective`` or ``annulled`` provision (for example with a
caution banner), and the finding is always kept (``test_verdict_is_pure_signal_never_drops``).

Honesty invariant: never claim ``current`` without evidence. A missing temporal column does not mean
"in force"; it means ``unknown``. A provision with no ``valid_to`` and no ``temporal_status`` could
be perpetual law or a row whose temporal data was never populated, so the honest default for "no
temporal evidence" is ``unknown`` (caveat, verify). ``current`` requires at least one piece of
positive in-window evidence (``test_no_temporal_data_is_unknown_never_current``).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

# The freshness verdict (a closed set):
# current           Positive evidence the provision is in force on the as-of date, and no closing or
#                   repeal signal.
# superseded        The validity window closed before the as-of date (valid_to < as_of). Stale.
# future_effective  Not yet in force on the as-of date (valid_from > as_of).
# annulled          temporal_status declares the provision repealed, annulled or abrogated.
# unknown           No temporal evidence to verify against: the honest default, never a silent
#                   "current".
Verdict = Literal["current", "superseded", "future_effective", "annulled", "unknown"]


# temporal_status markers. Two vocabularies coexist (see storage/models.py, migration 013, and
# analysis/provision_view.py):
#   * the app's canonical hyphenated set: 'in-force', 'future-effective', 'annulled', 'superseded';
#   * the engine's values, passed through verbatim (for example 'IN_FORCE', 'ANNULLED'), which
#     provision_view deliberately does not normalise into the hashed audit payload.
# To handle both, statuses are normalised (lower-cased, '-' and '_' turned into spaces, whitespace
# collapsed) and matched on substrings of the legally meaningful words rather than an exact enum, so
# 'ANNULLED', 'annulled', 'repealed' and 'abrogated' all read as repeal whichever producer set the
# column. An unrecognised status is not forced to a verdict: it falls through to the date checks
# and, without dates, to ``unknown``, never ``current``. Each marker therefore needs only its
# space-separated form ('in force' covers 'in-force', 'in_force' and 'in  force'; 'inforce' is
# listed separately). Order matters: annulled is checked first, since a repeal is decisive, which
# also resolves the overlap of 'valid' inside 'invalid'.
_ANNULLED_MARKERS = (
    "annul",
    "repeal",
    "abrogat",
    "revoke",
    "rescind",
    "void",
    "invalid",
    "nullif",
    "struck",
    "extinguish",
)  # repeal or annulment
_SUPERSEDED_MARKERS = (
    "supersed",
    "replaced",
    "expired",
    "ceased",
    "terminat",
    "lapsed",
    "obsolet",
)  # closed-by-replacement
_FUTURE_MARKERS = (
    "future effective",
    "futureeffective",
    "not yet in force",
    "not yet effective",  # the engine's NOT_YET_EFFECTIVE (otherwise read via "effective")
    "pending",
    "prospective",
)
_IN_FORCE_MARKERS = ("in force", "inforce", "current", "effective", "valid", "operative")


def _normalize_status(temporal_status: str | None) -> str:
    """Normalise a temporal_status for substring matching across the app and engine vocabularies.

    Lower-cases, turns '-' and '_' into spaces and collapses whitespace, so 'In-Force', 'in_force'
    and 'in  force' all become 'in force'. None or blank gives '' (no status declared).
    """
    s = (temporal_status or "").strip().lower().replace("-", " ").replace("_", " ")
    return " ".join(s.split())


def _status_is_annulled(status_norm: str) -> bool:
    """Return True if the normalised status declares a repeal (annulled, repealed, abrogated, ...).

    This is the one status reading that overrides the date checks, because a repeal is decisive
    even when the stored ``valid_to`` was never closed out.
    """
    return any(m in status_norm for m in _ANNULLED_MARKERS)


def temporal_freshness(
    valid_from: date | None,
    valid_to: date | None,
    temporal_status: str | None,
    analysis_as_of: date,
) -> dict:
    """Decide the temporal-freshness verdict for a cited provision as of an explicit date.

    Returns the ``payload['temporal_freshness']`` annotation dict::

        {
          "verdict": Verdict,             # see Verdict
          "reason": str,                  # explanation a lawyer can audit
          "stale_since": date | None,     # 'superseded' only: the valid_to that closed it
          "effective_as_of": date | None, # 'future_effective' only: the valid_from to come
          "analysis_as_of": date,         # echoed back so the annotation is self-describing
          "engine_version": int,
        }

    A signal, not a filter: it never drops, hides or demotes a finding, only labels it.

    ``current`` requires positive in-window evidence. With no temporal evidence at all (valid_from,
    valid_to and temporal_status all NULL or blank) the verdict is ``unknown``; a missing column
    never reads as "in force".

    Precedence, most decisive first:

    1. ``temporal_status`` declares a repeal: ``annulled`` (decisive even without dates).
    2. ``valid_to < as_of``: ``superseded``, with stale_since = valid_to.
    3. ``valid_from > as_of``: ``future_effective``, with effective_as_of = valid_from.
    4. Positive in-window evidence (a satisfied date bound or an explicit in-force status):
       ``current``.
    5. Otherwise: ``unknown``.

    The window is inclusive at both ends: ``valid_to == as_of`` is the last day in force (not
    superseded) and ``valid_from == as_of`` the first (not future_effective). Only a strict
    inequality triggers the stale or future branch.
    """
    status_norm = _normalize_status(temporal_status)

    base = {
        "stale_since": None,
        "effective_as_of": None,
        "analysis_as_of": analysis_as_of,
        "engine_version": 1,
    }

    # 1) A declared repeal or annulment is decisive and overrides the date window (a row can be
    #    repealed before its stored valid_to was ever closed out), so it is checked first.
    if _status_is_annulled(status_norm):
        return {
            "verdict": "annulled",
            "reason": f"temporal_status {temporal_status!r} declares the provision repealed/annulled",
            **base,
        }

    # 2) The validity window closed before the as-of date: superseded. The end is inclusive
    #    (valid_to == as_of is the last day in force), hence the strict `<`. Checked before the
    #    future branch so a malformed row with valid_to < valid_from still reads as stale.
    if valid_to is not None and valid_to < analysis_as_of:
        return {
            "verdict": "superseded",
            "reason": f"validity window closed on {valid_to.isoformat()}, before the analysis date {analysis_as_of.isoformat()}",
            **{**base, "stale_since": valid_to},
        }

    # 3) Not yet in force on the as-of date: future_effective. The start is inclusive
    #    (valid_from == as_of is the first day in force), hence the strict `>`.
    if valid_from is not None and valid_from > analysis_as_of:
        return {
            "verdict": "future_effective",
            "reason": f"becomes effective on {valid_from.isoformat()}, after the analysis date {analysis_as_of.isoformat()}",
            **{**base, "effective_as_of": valid_from},
        }

    # 4) `current` needs positive in-window evidence and is never inferred from absence. Any of:
    #      * a valid_from on or before the as-of date (the window has opened; step 2 already ruled
    #        out a closed valid_to);
    #      * a valid_to on or after the as-of date (we are within the window's tail);
    #      * an explicit in-force temporal_status (the producer marked it current, effective or
    #        valid).
    has_open_lower_bound = valid_from is not None and valid_from <= analysis_as_of
    has_open_upper_bound = valid_to is not None and valid_to >= analysis_as_of
    status_in_force = bool(status_norm) and any(m in status_norm for m in _IN_FORCE_MARKERS)
    status_future = bool(status_norm) and any(m in status_norm for m in _FUTURE_MARKERS)
    status_superseded = bool(status_norm) and any(m in status_norm for m in _SUPERSEDED_MARKERS)

    # An explicit 'future-effective' status with no date to check against is future, not current.
    if status_future and valid_from is None and valid_to is None:
        return {
            "verdict": "future_effective",
            "reason": f"temporal_status {temporal_status!r} declares the provision not yet in force (no effective date supplied)",
            **base,
        }

    # Likewise an explicit superseded / expired / replaced status with no date to check against is
    # stale, not unknown. (When dates are present they decide, as in steps 2 to 4.)
    if status_superseded and valid_from is None and valid_to is None:
        return {
            "verdict": "superseded",
            "reason": f"temporal_status {temporal_status!r} declares the provision superseded (no end date supplied)",
            **base,
        }

    if has_open_lower_bound or has_open_upper_bound or status_in_force:
        return {
            "verdict": "current",
            "reason": _current_reason(
                has_open_lower_bound, has_open_upper_bound, status_in_force, analysis_as_of
            ),
            **base,
        }

    # 5) No verifiable temporal evidence (all NULL or blank, or an unrecognised status without
    #    dates): `unknown`, never `current` from a missing column.
    return {
        "verdict": "unknown",
        "reason": "no verifiable temporal data (valid_from / valid_to / temporal_status all absent or unrecognised) — cannot assert the provision is current",
        **base,
    }


def _current_reason(lower: bool, upper: bool, status: bool, as_of: date) -> str:
    """Build the audit reason for a ``current`` verdict from the positive evidence behind it."""
    parts = []
    if lower:
        parts.append("the validity window had opened")
    if upper:
        parts.append("the validity window had not yet closed")
    if status:
        parts.append("temporal_status marks it in force")
    return f"in force as of {as_of.isoformat()}: " + "; ".join(parts)
