"""The AnswerContext value object: business-identity signals for a legal answer.

Carries what an answer path needs to scope, gate or personalise a legal answer: legal form, NACE
code, derived tags, size bands, jurisdiction, the five compliance gate booleans, municipality and
preferred output language. It is shared by the answer-context assembler and the answer path (the
/api/answer handler builds one from a saved business profile when profile context is enabled).
Nothing here calls the database, the engine or an LLM.

``context_hash`` is a stable sha256 over a canonical JSON of the fields (sorted keys), suitable as
an audit or cache key: two contexts with equal field values hash identically regardless of
construction order.

``from_business_profile`` maps a ``storage.models.BusinessProfile`` row (or any duck-typed stub
exposing the same attributes) to an AnswerContext, tolerating missing fields.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


def _turnover_band(annual_turnover_czk: int | None) -> str | None:
    """Bucket an annual CZK turnover into a coarse, stable band.

    Returns None when the turnover is unknown, so a missing value never invents a band.
    """
    if annual_turnover_czk is None:
        return None
    try:
        v = int(annual_turnover_czk)
    except (TypeError, ValueError):
        return None
    if v < 2_000_000:
        return "<2M"
    if v < 10_000_000:
        return "2M-10M"
    if v < 50_000_000:
        return "10M-50M"
    if v < 250_000_000:
        return "50M-250M"
    return "250M+"


@dataclass(frozen=True)
class AnswerContext:
    """Immutable business-identity context for an answer.

    Every field has a null or empty default, so a partially populated context is always
    constructible. ``derived_tags`` is a list; the five gate booleans default to False; bands,
    codes and language default to None.
    """

    legal_form: str | None = None
    nace_code: str | None = None
    derived_tags: list[str] = field(default_factory=list)
    headcount_band: str | None = None
    turnover_band: str | None = None
    jurisdiction: str | None = None
    # The five compliance gate booleans.
    processes_personal_data: bool = False
    handles_client_money: bool = False
    has_real_estate: bool = False
    cross_border: bool = False
    is_employer: bool = False
    municipality_code: str | None = None
    preferred_language: str | None = None

    @property
    def context_hash(self) -> str:
        """Return a stable sha256 over a canonical JSON of the fields.

        Deterministic and independent of construction order: ``derived_tags`` keeps its order
        (it is semantically ordered), but the JSON keys are sorted, so two contexts with equal
        field values always hash identically.
        """
        canonical = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def from_business_profile(cls, profile: Any) -> AnswerContext:
        """Build an AnswerContext from a BusinessProfile row or duck-typed stub.

        Any absent attribute falls back to its default. ``derived_tags`` comes from
        ``analysis.profile_tags.derive_tags_for_profile`` (sector then activity tags,
        de-duplicated in order); if that raises, it falls back to an empty list so a malformed
        stub never breaks construction.
        """

        def _g(attr: str, default: Any = None) -> Any:
            return getattr(profile, attr, default)

        try:
            from regsuper.analysis.profile_tags import derive_tags_for_profile

            sector_tags, activity_tags = derive_tags_for_profile(profile)
            seen: set[str] = set()
            derived_tags = [
                t
                for t in (list(sector_tags) + list(activity_tags))
                if not (t in seen or seen.add(t))
            ]
        except Exception:
            derived_tags = []

        return cls(
            legal_form=_g("legal_form"),
            nace_code=_g("nace_code"),
            derived_tags=derived_tags,
            headcount_band=_g("headcount_band"),
            turnover_band=_turnover_band(_g("annual_turnover_czk")),
            jurisdiction=_g("primary_jurisdiction"),
            processes_personal_data=bool(_g("processes_personal_data", False)),
            handles_client_money=bool(_g("handles_client_money", False)),
            has_real_estate=bool(_g("has_real_estate", False)),
            cross_border=bool(_g("cross_border", False)),
            is_employer=bool(_g("is_employer", False)),
            # Prefer the validated ČSÚ municipality (obec) code; fall back to the free-text
            # `registered_municipality` on older profile rows that predate the code field.
            municipality_code=_g("municipality_code") or _g("registered_municipality"),
            preferred_language=_g("preferred_language"),
        )
