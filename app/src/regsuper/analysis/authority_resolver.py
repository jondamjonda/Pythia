"""Competent-authority resolver: which office does the asker file with?

Answers the practical "kam podat / který úřad" (where to file, which office) question by turning a
topic and the asker's municipality into the concrete competent office or offices:

    topic_key, via the topic map             -> authority type(s)
    municipality_code, via the gazetteer row -> okres / orp / kraj area codes
    (authority type, area), via jurisdiction -> concrete authority row(s)

This module is deterministic glue over three existing pieces: the ``authority_vocab`` tags, the
``authority_registry`` seed shape and the municipality gazetteer. It:

* never touches the network or writes the live database; it works on an in-memory index built
  from the local seed (the same ``ParsedSeed`` the registry loader produces) plus a gazetteer
  municipality row (a plain dict);
* never raises: ``resolve`` returns ``[]`` when nothing matches and never guesses an office;
* is off by default: the answer slot only uses it behind the ``authority_registry_enabled`` flag
  (see ``maybe_resolve_authorities``).

All authority data behind it is a draft pending advocate review (see the
``tests/fixtures/authority/*.sample.json`` seeds). Fetching the real directory and loading the live
``authority`` and ``authority_jurisdiction`` tables stays an operator action
(``authority_registry.upsert_seed``, not invoked here).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from regsuper.analysis import authority_vocab as vocab
from regsuper.ingest.authority_registry import ParsedSeed, parse_seed

# The three gazetteer grains, most specific first. For the same municipality and
# type, an office whose jurisdiction is stamped at a finer grain (okres) is
# preferred over one stamped at a coarser grain (kraj).
_AREA_GRAINS: tuple[str, ...] = ("okres", "orp", "kraj")

# Map each grain to the gazetteer-row key that carries that grain's area code.
_GRAIN_TO_GAZ_KEY: dict[str, str] = {
    "okres": "okres_code",
    "orp": "orp_code",
    "kraj": "kraj_code",
}


@dataclass(frozen=True)
class ResolvedAuthority:
    """One concrete competent office returned by the resolver.

    Attributes:
        authority_type: The shared vocabulary tag it was matched on.
        code, name: The concrete office, from the registry seed.
        address, databox, source_uri: Contact details and provenance, passed through (nullable).
        match_kind: How it matched: 'national' (no jurisdiction, so competent everywhere for its
            type) or one of 'okres', 'orp', 'kraj' (a territorial match at that grain).
        area_code: The area code the territorial match used (None for national).
        is_draft: Always True here, because the seed is a draft pending advocate review.
    """

    authority_type: str
    code: str
    name: str
    match_kind: str
    area_code: str | None = None
    address: str | None = None
    databox: str | None = None
    source_uri: str | None = None
    is_draft: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Topic to authority-type map
# ─────────────────────────────────────────────────────────────────────────────
def parse_topic_map(raw: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Turn a ``{"topics": {topic_key: [type, ...]}}`` mapping into ``{topic_key: (tag, ...)}``.

    Each listed authority type is normalised through the shared vocabulary
    (``normalize_tag``). Unknown types are dropped (so a typo can never produce a
    tag that joins nothing), duplicates are removed preserving priority order,
    and a topic left with no valid types is dropped entirely. Never raises; a
    malformed shape yields ``{}``.
    """
    topics = raw.get("topics") if isinstance(raw, Mapping) else None
    if not isinstance(topics, Mapping):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for topic_key, types in topics.items():
        if not isinstance(topic_key, str) or not topic_key.strip():
            continue
        if not isinstance(types, (list, tuple)):
            continue
        seen: list[str] = []
        for t in types:
            tag = vocab.normalize_tag(t if isinstance(t, str) else None)
            if tag and tag not in seen:
                seen.append(tag)
        if seen:
            out[topic_key.strip()] = tuple(seen)
    return out


def load_topic_map(path: Path | str) -> dict[str, tuple[str, ...]]:
    """Read and parse the topic-to-authority-type JSON fixture into a clean map."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_topic_map(data)


# ─────────────────────────────────────────────────────────────────────────────
# In-memory registry index (built from the local seed, no database)
# ─────────────────────────────────────────────────────────────────────────────
class AuthorityIndex:
    """An in-memory index over a ``ParsedSeed`` for territorial lookup.

    Two buckets, both built from the seed alone:

    * ``by_area[(area_kind, area_code)]``: authority-row dicts whose jurisdiction
      covers that area.
    * ``national_by_type[authority_type]``: authorities with no jurisdiction at
      all (competent everywhere for their type).

    Building it touches no database and no network.
    """

    __slots__ = ("_by_code", "by_area", "national_by_type")

    def __init__(self, parsed: ParsedSeed) -> None:
        self._by_code: dict[str, dict[str, str | None]] = {
            a["code"]: a for a in parsed.authorities if a.get("code")
        }
        self.by_area: dict[tuple[str, str], list[dict[str, str | None]]] = {}
        # authority_code mapped to True if it has at least one jurisdiction (so is not national).
        has_jur: dict[str, bool] = {}
        for j in parsed.jurisdictions:
            code = j.get("authority_code")
            kind = j.get("area_kind")
            area = j.get("area_code")
            if not code or not kind or not area or code not in self._by_code:
                continue
            has_jur[code] = True
            self.by_area.setdefault((kind, area), []).append(self._by_code[code])

        self.national_by_type: dict[str, list[dict[str, str | None]]] = {}
        for code, auth in self._by_code.items():
            if has_jur.get(code):
                continue
            atype = auth.get("authority_type")
            if atype:
                self.national_by_type.setdefault(atype, []).append(auth)


def build_index(parsed: ParsedSeed) -> AuthorityIndex:
    """Build the in-memory lookup index from a parsed seed (no database access)."""
    return AuthorityIndex(parsed)


# ─────────────────────────────────────────────────────────────────────────────
# The resolver
# ─────────────────────────────────────────────────────────────────────────────
def _row_to_resolved(
    auth: Mapping[str, Any], match_kind: str, area_code: str | None
) -> ResolvedAuthority:
    return ResolvedAuthority(
        authority_type=str(auth.get("authority_type")),
        code=str(auth.get("code")),
        name=str(auth.get("name")),
        match_kind=match_kind,
        area_code=area_code,
        address=auth.get("address"),
        databox=auth.get("databox"),
        source_uri=auth.get("source_uri"),
        is_draft=True,
    )


def resolve(
    topic_key: str | None,
    municipality: Mapping[str, Any] | None,
    index: AuthorityIndex,
    topic_map: Mapping[str, tuple[str, ...]],
) -> list[ResolvedAuthority]:
    """Resolve a topic, and optionally a municipality gazetteer row, to concrete authorities.

    Deterministic and never raises: returns ``[]`` on any non-match (unknown
    topic, unmapped area, empty registry). It never guesses.

    For each authority type the topic maps to, in the topic's priority order:

    1. National authorities of that type (no jurisdiction) are always competent
       and are emitted with ``match_kind='national'``.
    2. If a ``municipality`` gazetteer row is given, derive its okres, orp and
       kraj area codes and take the finest-grain territorial match available
       for that type (okres beats orp beats kraj). Without a municipality only
       national matches are returned.

    Results are de-duplicated on office ``code`` (the first, most specific match
    is kept) and grouped by type in the topic's priority order.
    """
    if not topic_key:
        return []
    types = topic_map.get(topic_key.strip()) if isinstance(topic_key, str) else None
    if not types:
        return []

    # Derive (grain, area_code) pairs for this municipality, finest first.
    area_lookups: list[tuple[str, str]] = []
    if isinstance(municipality, Mapping):
        for grain in _AREA_GRAINS:
            code = municipality.get(_GRAIN_TO_GAZ_KEY[grain])
            if isinstance(code, str) and code.strip():
                area_lookups.append((grain, code.strip()))

    out: list[ResolvedAuthority] = []
    seen_codes: set[str] = set()

    for atype in types:
        # 1) National offices of this type are always competent.
        for auth in index.national_by_type.get(atype, []):
            code = auth.get("code")
            if code and code not in seen_codes:
                seen_codes.add(code)
                out.append(_row_to_resolved(auth, "national", None))

        # 2) Finest-grain territorial match for this type.
        for grain, area_code in area_lookups:
            bucket = index.by_area.get((grain, area_code), [])
            matched_here = False
            for auth in bucket:
                if auth.get("authority_type") != atype:
                    continue
                code = auth.get("code")
                if code and code not in seen_codes:
                    seen_codes.add(code)
                    out.append(_row_to_resolved(auth, grain, area_code))
                matched_here = True
            # The finest grain that produced a match wins; stop climbing to coarser
            # grains for this type (an okres office pre-empts its kraj office).
            if matched_here:
                break

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Flag-gated entry point (off by default)
# ─────────────────────────────────────────────────────────────────────────────
def maybe_resolve_authorities(
    topic_key: str | None,
    municipality: Mapping[str, Any] | None,
    index: AuthorityIndex | None,
    topic_map: Mapping[str, tuple[str, ...]] | None,
    *,
    enabled: bool,
) -> list[ResolvedAuthority]:
    """Resolve authorities for the answer slot; return ``[]`` unless ``enabled``.

    With the ``authority_registry_enabled`` flag off (the default) this is a
    no-op and the practical "where to file" slot is left empty. Only when the
    flag is on and a built ``index`` and ``topic_map`` are supplied does it
    delegate to :func:`resolve`.
    """
    if not enabled or index is None or topic_map is None:
        return []
    return resolve(topic_key, municipality, index, topic_map)


# ─────────────────────────────────────────────────────────────────────────────
# Offline convenience: resolve straight from the draft sample fixtures
# ─────────────────────────────────────────────────────────────────────────────
_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "authority"
_SAMPLE_REGISTRY = _FIXTURE_DIR / "authority_registry.sample.json"
_SAMPLE_TOPICS = _FIXTURE_DIR / "topic_authority.sample.json"
_SAMPLE_GAZETTEER = _FIXTURE_DIR / "municipality_gazetteer.sample.json"


def _load_sample_gazetteer() -> dict[str, dict[str, Any]]:
    data = json.loads(_SAMPLE_GAZETTEER.read_text(encoding="utf-8"))
    return {row["code"]: row for row in data if isinstance(row, dict) and row.get("code")}


def resolve_from_fixtures(
    topic_key: str | None, municipality_code: str | None = None
) -> list[ResolvedAuthority]:
    """Resolve against the draft sample fixtures, fully offline.

    Loads the sample registry seed, topic map and small gazetteer slice from
    ``tests/fixtures/authority`` and resolves against them, with no database or
    network. Used by tests and by a local answer-slot demo. An unknown
    ``municipality_code`` is treated as no municipality (national matches only),
    and an unknown topic yields ``[]``.
    """
    index = build_index(parse_seed(json.loads(_SAMPLE_REGISTRY.read_text(encoding="utf-8"))))
    topic_map = load_topic_map(_SAMPLE_TOPICS)
    muni = _load_sample_gazetteer().get(municipality_code) if municipality_code else None
    return resolve(topic_key, muni, index, topic_map)
