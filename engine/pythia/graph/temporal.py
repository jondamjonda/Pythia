"""Temporal metadata and stable identifiers for provisions (pure, database-free).

Two precautions, in the style of `taxonomy.py`:

  - Time: a citation must be answerable as of a point in time, and the engine must never present
    a consolidated text as authentic. So it keeps `valid_from`/`valid_to` (účinnost, effectiveness)
    separate from `platnost` (formal validity), an `is_authoritative` flag (the consolidated úplné
    znění is informative only until e-Legislativa), and `annulment_effect` (whether a Constitutional
    Court annulment applies ex nunc or ex tunc).
  - Identifiers: e-Sbírka declared its `frag_` ids unstable until 15 January 2026, so they are not
    the canonical identifier. The stable locator is (act code "n/year Sb.", label "§ N",
    structural path), which still resolves if the fragment id changes. The fragment id is a
    convenience, trusted only for data fetched after the cutoff.

The schema and population live in `pythia/ingest/temporal_complete.py`.
"""

from __future__ import annotations

from datetime import date, datetime

# Cutoff stated by the e-Sbírka operator: fragment ids may change until this date and can be trusted
# after it. Data fetched earlier must be re-resolved through the stable structural path.
FRAG_STABLE_AFTER = date(2026, 1, 15)

# Timing of a Constitutional Court annulment (derogation).
ANNULMENT_EFFECT: tuple[str, ...] = ("EX_NUNC", "EX_TUNC")  # from the ruling on, or retroactive

# The caveat attached to non-authoritative consolidated text and shown to users.
CONSOLIDATED_CAVEAT = (
    "Consolidated úplné znění — informative only until e-Legislativa; the binding form is the "
    "promulgated PDF částka (Sbírka zákonů) or, for ČNB, the Věstník ČNB."
)


def is_authoritative(typ_zneni: str | None) -> bool:
    """Return whether a text version is authentic rather than an informative consolidation.

    Consolidated text (e-Sbírka `KONSOL` znění) is informative only until e-Legislativa. Unknown
    provenance returns False, the safe default: show the caveat unless the text is proven binding.
    """
    if not typ_zneni:
        return False
    return "KONSOL" not in typ_zneni.upper()


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def frags_trustworthy(fetched_at) -> bool:
    """Return whether data was fetched on or after the cutoff, so its `frag_`/`eid` ids are stable.

    Earlier data must be re-resolved through the stable locator; see `needs_reresolution`.
    """
    d = _as_date(fetched_at)
    return d is not None and d >= FRAG_STABLE_AFTER


def needs_reresolution(fetched_at) -> bool:
    """Return whether a document's fragment ids predate the cutoff and need re-resolving.

    Such documents must be re-resolved against the current e-Sbírka structure by the scheduled
    maintenance task.
    """
    d = _as_date(fetched_at)
    return d is None or d < FRAG_STABLE_AFTER


def stable_locator(external_id: str, label: str | None, hierarchy_path: str | None) -> dict:
    """Return the canonical identifier of a provision, never the volatile fragment id.

    The locator (act "n/year Sb.", label "§ N", structural ltree path) resolves a citation even if
    the e-Sbírka fragment id changes.
    """
    return {"act": external_id, "label": label, "path": hierarchy_path}


def eli_path(external_id: str | None, version_date: str | None = None) -> str | None:
    """Return the ELI URI path for an act, scoped to a version if a version date is given.

    The e-Sbírka external_id 'number/year' (e.g. '21/1992') maps to
    /eli/cz/sb/{year}/{number}[/{version_date}].
    """
    if not external_id or "/" not in external_id:
        return None
    number, year = external_id.split("/", 1)
    year = year.split()[0]  # strip a trailing ' Sb.' if present
    base = f"/eli/cz/sb/{year}/{number}"
    return f"{base}/{version_date}" if version_date else base
