"""Read-only municipality search (`GET /api/municipalities?q=<prefix>`) for autocomplete.

The profile form's town field uses it so the stored `business_profile.municipality_code` is always
a real gazetteer code, never free text; saving a profile still validates the code (see
`api/profiles.py::_validate_municipality_code`). The gazetteer is public reference data without
row-level security, but the route is authenticated like its neighbours.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import text as sql_text

from regsuper.api.auth import get_principal
from regsuper.storage.database import session_scope

router = APIRouter(prefix="/api/municipalities", tags=["municipalities"])

# Result cap: the autocomplete only ever needs a short list.
_MAX_ROWS = 10

# Diacritic fold: Czech accented letters mapped to their ASCII base. Postgres applies it as
# `translate(lower(x), _FROM, _TO)` to both the name and the query, so matching is
# accent-insensitive without the `unaccent` extension (which is not guaranteed to be installed).
# Text is lower-cased first, so only the lowercase accented forms need listing.
_FOLD_FROM = "áčďéěíňóřšťúůýžäöü"
_FOLD_TO = "acdeeinorstuuyzaou"

# Case- and diacritic-insensitive prefix search: the folded name is compared with a folded prefix.
# An exact match sorts first, then prefix matches alphabetically. All values are bound parameters.
_SEARCH_SQL = f"""
    SELECT code, name, okres_name, orp_name, kraj_name
      FROM municipality
     WHERE translate(lower(name), :fold_from, :fold_to) LIKE :prefix
     ORDER BY
       CASE WHEN translate(lower(name), :fold_from, :fold_to) = :exact THEN 0 ELSE 1 END,
       name
     LIMIT {_MAX_ROWS}
"""


class MunicipalityMatch(BaseModel):
    """One gazetteer match.

    `code` is the value the form stores. The district (okres), municipality with extended
    powers (ORP) and region (kraj) names disambiguate municipalities that share a name.
    """

    name: str
    code: str
    okres: str | None
    orp: str | None
    kraj: str | None


def _fold(value: str) -> str:
    """Lower-case and strip Czech diacritics, mirroring the SQL `translate(lower(...))`."""
    out = value.lower()
    return out.translate(str.maketrans(_FOLD_FROM, _FOLD_TO))


@router.get("", response_model=list[MunicipalityMatch])
async def search_municipalities(
    request: Request,
    q: str = Query(default="", description="Case/diacritic-insensitive name prefix."),
) -> list[MunicipalityMatch]:
    """Return up to 10 municipalities whose name starts with `q`.

    Matching ignores case and diacritics, so "plz", "PLZ" and "plž" all match "Plzeň". An exact
    match comes first, then the rest alphabetically. A blank `q` returns an empty list. Returns
    401 for an unauthenticated caller when authentication is required.
    """
    # Resolve the principal before any read (401 when auth is required and missing). The
    # gazetteer itself is public, so no org filter is applied.
    await get_principal(request)

    folded = _fold(q).strip()
    if not folded:
        return []

    async with session_scope() as session:
        rows = (
            await session.execute(
                sql_text(_SEARCH_SQL),
                {
                    "fold_from": _FOLD_FROM,
                    "fold_to": _FOLD_TO,
                    "prefix": f"{folded}%",
                    "exact": folded,
                },
            )
        ).all()

    # SELECT order is: code, name, okres_name, orp_name, kraj_name.
    return [
        MunicipalityMatch(
            code=r[0],
            name=r[1],
            okres=r[2],
            orp=r[3],
            kraj=r[4],
        )
        for r in rows
    ]
