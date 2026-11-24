"""The provenance bundle: a validated record of where each instrument and version came from.

Pure and database-free, like `temporal.py` and `taxonomy.py`. The bundle is a fixed-shape JSONB
object stored on `source_document.provenance` and carried into `provision_version`. Two rules
matter most:

- Dates keep their meaning. Sources report different dates in the same place: *účinnost* (effect,
  e-Sbírka), the *ze dne* signature date (ČNB), or nothing (EU, regional law). `build()` maps each
  source's date to the right slot (`date_adopted` or `valid_from`, the effective date) and never
  invents an unknown date.
- Authoritativeness depends on the source family. The EU Formex, ČNB PDF and regional ODT primary
  forms are authentic (True); only the consolidated e-Sbírka text (*úplné znění*) is informative
  only (False).

Version history is keyed by (source_document_id, provision_label), which is unique per document.
"""

from __future__ import annotations

from datetime import date

# Source collections: the connector or official channel a document came from.
SOURCES = ("esbirka", "eurlex", "usc", "cnb", "court")
BASELINES = ("adopted", "consolidated")  # as adopted / published, or a consolidated úplné znění
TEMPORAL_STATUS = ("IN_FORCE", "NOT_YET_EFFECTIVE", "REPEALED", "CONSOLIDATED", "AMENDING", "ANNULLED_BY_CC", "UNKNOWN")

# The keys every provenance bundle must carry (None where genuinely unknown).
# `date_application` is the date from which soft law says it applies ("se postupuje od ..."). It is
# legally distinct from date_adopted (the signature date) and valid_from (a statutory effective date),
# so it has its own slot. It must never be copied into valid_from, which would present soft law as
# hard law.
PROVENANCE_KEYS = frozenset(
    {
        "official_id",
        "title",
        "institution",
        "source",
        "language",
        "date_adopted",
        "date_published",
        "valid_from",
        "valid_to",
        "platnost",
        "date_application",
        "temporal_status",
        "is_authoritative",
        "baseline",
        "enabling_act_id",
        "raw_checksum",
        "raw_uri",
        "fetched_at",
    }
)


# Apex courts whose own portals (NALUS, NS, NSS) publish authentic ruling text.
COURT_PUBLISHERS = ("Ústavní soud", "Nejvyšší soud", "Nejvyšší správní soud")


def source_of(publisher: str | None) -> str:
    """Map a publisher name to its source collection.

    Court rulings get their own source, 'court', because they are not primary law. A publisher is a
    court when it names an apex court (Constitutional, Supreme or Supreme Administrative Court) or
    follows the Ministry of Justice portal convention ``'Soud:<court name>'`` (like the ``'ÚSC:'``
    prefix for regional law). The court check must come before the ``.get(p, 'esbirka')`` fallback,
    or a ruling would be silently filed as an e-Sbírka statute.
    """
    p = publisher or ""
    if p.startswith("ÚSC:"):
        return "usc"
    if p.startswith("Soud:") or p in COURT_PUBLISHERS:
        return "court"
    return {"Sbírka zákonů": "esbirka", "EUR-Lex": "eurlex", "ČNB": "cnb"}.get(p, "esbirka")


def is_authoritative_for(source: str, typ_zneni: str | None, bootstrap: bool = False) -> bool:
    """Return whether a document's text is the authentic, legally binding form for its source.

    Only consolidated e-Sbírka text (KONSOL) is informative only. The EU Formex, the ČNB published
    PDF and regional authorities' published office files are authentic primary forms.

    For court rulings, text from the official portals (NALUS, NS, NSS, the Ministry of Justice
    portal) is authoritative. A record bootstrapped from the CzCDC dataset (flagged in metadata,
    not taken from an official portal) is informative only; `bootstrap` selects that case.
    """
    if source == "esbirka":
        return "KONSOL" not in (typ_zneni or "KONSOL")  # unknown typ_zneni counts as consolidated
    if source == "court":
        return not bootstrap  # official portal text is authoritative; bootstrapped records are not
    return True


def baseline_for(source: str) -> str:
    """Return the text baseline: 'consolidated' for e-Sbírka, 'adopted' for every other source."""
    return "consolidated" if source == "esbirka" else "adopted"


def institution_of(source: str, publisher: str | None, issuing_authority: str | None) -> str:
    """Return the issuing institution.

    For regional law (where the stored column says 'neurčeno', "unspecified") and for court rulings
    (where the deciding court is the publisher and issuing_authority is empty), it is derived from
    the publisher name.
    """
    if source == "usc" and (publisher or "").startswith("ÚSC:"):
        return publisher[len("ÚSC:") :]  # e.g. 'Hlavní město Praha', 'Liberecký kraj'
    if source == "court":
        p = publisher or ""
        return p[len("Soud:") :] if p.startswith("Soud:") else (p or "neurčeno")  # the deciding court
    return issuing_authority or {"eurlex": "EU", "cnb": "ČNB"}.get(source, "neurčeno")


def _iso(d) -> str | None:
    return d.isoformat() if isinstance(d, date) else (d or None)


def build(
    *,
    external_id: str,
    title: str | None,
    publisher: str | None,
    language: str | None,
    issuing_authority: str | None,
    valid_from,
    valid_to,
    temporal_status: str | None,
    raw_checksum: str | None,
    raw_storage_uri: str | None,
    fetched_at,
    bindingness: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Assemble the provenance bundle from a source_document's fields and metadata.

    Applies the per-source authoritativeness rule and the per-source date mapping; unknown dates
    stay None rather than being invented.
    """
    md = metadata or {}
    src = source_of(publisher)
    # Date mapping. For ČNB, valid_from holds the 'ze dne' signature date, so it becomes
    # date_adopted, and soft-law notices (úřední sdělení) have no separate effective date. For every
    # other source valid_from really is the effective date (účinnost). Exception: a binding ČNB
    # measure of general nature (opatření obecné povahy) has a real statutory effective date parsed
    # from its text; when present, that is valid_from, not date_adopted.
    if src == "cnb" and not (bindingness == "BINDING_OOP" and valid_from is not None):
        date_adopted, eff_from = _iso(valid_from), None
    elif src == "court":
        # A ruling speaks from its decision date, which is both its adoption date and valid_from.
        # valid_from stays set (unlike ČNB soft law) so the point-in-time mask ("rulings as of D")
        # keeps the ruling; valid_to stays NULL because a ruling is not repealed.
        date_adopted = eff_from = _iso(valid_from)
    else:
        date_adopted, eff_from = (_iso(md.get("date_adopted")) if md.get("date_adopted") else None, _iso(valid_from))
    return {
        "official_id": external_id,
        "title": title,
        "institution": institution_of(src, publisher, issuing_authority),
        "source": src,
        "language": language,
        "date_adopted": date_adopted,
        "date_published": _iso(md.get("date_published")) if md.get("date_published") else None,
        "valid_from": eff_from,
        "valid_to": _iso(valid_to),
        "platnost": None,  # formal validity; no source provides it yet
        "date_application": _iso(md.get("date_application")) if md.get("date_application") else None,
        "temporal_status": temporal_status if temporal_status in TEMPORAL_STATUS else "UNKNOWN",
        "is_authoritative": is_authoritative_for(src, md.get("typ_zneni"), bool(md.get("bootstrap"))),
        "baseline": baseline_for(src),
        "enabling_act_id": None,  # decree-to-statute link, not populated yet (see delegates_decree)
        "raw_checksum": raw_checksum,
        "raw_uri": raw_storage_uri,
        "fetched_at": _iso(fetched_at),
    }


def is_valid(bundle: dict) -> bool:
    """Return True if the bundle has exactly the expected keys, with valid values and date formats."""
    if set(bundle) != PROVENANCE_KEYS:
        return False
    if bundle["source"] not in SOURCES or bundle["baseline"] not in BASELINES:
        return False
    if bundle["temporal_status"] not in TEMPORAL_STATUS or not isinstance(bundle["is_authoritative"], bool):
        return False
    for k in ("date_adopted", "date_published", "valid_from", "valid_to", "platnost", "date_application"):
        v = bundle[k]
        if v is not None and not _is_iso_date(v):
            return False
    return True


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(str(s)[:10])
        return True
    except (ValueError, TypeError):
        return False
