"""Load the competent-authority directory and its jurisdiction map into the database.

Fills `authority` with concrete offices (tax offices, social security offices, KHS, OIP, trade
licensing offices and so on) and `authority_jurisdiction` with the map from administrative area
to office. Every run upserts on the primary keys, so re-runs converge both tables to the seed.

Usage (from app/, with secrets.env loaded and migration 022 applied):
    .venv/bin/python -m regsuper.ingest.authority_registry [--seed PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from regsuper.analysis.authority_vocab import normalize_tag
from regsuper.storage.database import session_scope

# app/data/geo/authority_registry.json: this file lives in src/regsuper/ingest/, so the fourth
# parent directory is the app root.
#
# The loader reads only this local seed; fetching the official directories is an operator step.
# The seed is built from these public directories, converting each (CSV or HTML) into rows of
# the shape documented on `parse_seed`, with `source_uri` set to the directory URL and
# `fetched_at` to the fetch date:
#
#   * Finanční správa, tax offices (FÚ, ÚzP):
#     https://www.financnisprava.cz/cs/financni-sprava/organy-financni-spravy
#   * ČSSZ, social security offices (OSSZ, PSSZ, MSSZ): https://www.cssz.cz/web/cz/kontakty
#   * SÚIP, regional labour inspectorates (OIP):
#     https://www.suip.cz/web/suip/oblastni-inspektoraty-prace
#   * KHS, regional public health stations (one per kraj): links at https://www.mzcr.cz/
#   * Trade licensing offices (živnostenské úřady): https://www.rzp.cz/
DEFAULT_SEED = Path(__file__).resolve().parents[3] / "data" / "geo" / "authority_registry.json"

_VALID_AREA_KINDS = frozenset({"okres", "orp", "kraj"})

_AUTHORITY_COLUMNS = (
    "code",
    "authority_type",
    "name",
    "address",
    "databox",
    "source_uri",
    "fetched_at",
)


def _clean(value: Any) -> str | None:
    """Normalise a cell to a stripped string, or None when empty; non-strings are stringified."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


class ParsedSeed:
    """The two flat row lists, ready for the two upserts.

    `authorities` holds dicts keyed by ``_AUTHORITY_COLUMNS``; `jurisdictions` holds dicts of
    {authority_code, area_kind, area_code}.
    """

    __slots__ = ("authorities", "jurisdictions")

    def __init__(
        self,
        authorities: list[dict[str, str | None]],
        jurisdictions: list[dict[str, str | None]],
    ) -> None:
        self.authorities = authorities
        self.jurisdictions = jurisdictions


def parse_seed(raw: list[dict[str, Any]]) -> ParsedSeed:
    """Normalise seed dicts into authority and jurisdiction rows without touching the database.

    The seed is a list of authority objects, each carrying its jurisdictions inline::

        {
          "code": "FU-JMK-BRNO-VENKOV",
          "authority_type": "financni_urad",        # tag, or a Czech label or alias
          "name": "Finanční úřad pro Jihomoravský kraj, ÚzP Brno-venkov",
          "address": "Příkop 25, 602 00 Brno",
          "databox": "np48kfp",
          "source_uri": "https://www.financnisprava.cz/...",
          "fetched_at": "2026-07-03",
          "jurisdictions": [
            {"area_kind": "okres", "area_code": "CZ0643"},
            {"area_kind": "orp",   "area_code": "6208"}
          ]
        }

    Rules:
      - ``code``, ``authority_type`` and ``name`` are required; a row missing any of them is
        skipped (``code`` is the primary key, the other two are NOT NULL).
      - ``authority_type`` is normalised through the shared vocabulary (``normalize_tag``), so a
        directory converted from CSV can keep human labels. An unrecognised type skips the
        whole authority, so a typo can never store a tag that joins to nothing.
      - Authorities are de-duplicated on ``code`` (last one wins).
      - Each inline ``jurisdictions`` entry becomes a jurisdiction row. Entries with an unknown
        ``area_kind`` or a blank ``area_code`` are dropped, and rows are de-duplicated on their
        (authority_code, area_kind, area_code) primary key.
    """
    by_code: dict[str, dict[str, str | None]] = {}
    jur_by_key: dict[tuple[str, str, str], dict[str, str | None]] = {}

    for item in raw:
        code = _clean(item.get("code"))
        name = _clean(item.get("name"))
        atype = normalize_tag(_clean(item.get("authority_type")))
        if not code or not name or not atype:
            continue

        by_code[code] = {
            "code": code,
            "authority_type": atype,
            "name": name,
            "address": _clean(item.get("address")),
            "databox": _clean(item.get("databox")),
            "source_uri": _clean(item.get("source_uri")),
            "fetched_at": _clean(item.get("fetched_at")),
        }

        for jur in item.get("jurisdictions") or []:
            if not isinstance(jur, dict):
                continue
            area_kind = _clean(jur.get("area_kind"))
            area_code = _clean(jur.get("area_code"))
            if area_kind not in _VALID_AREA_KINDS or not area_code:
                continue
            key = (code, area_kind, area_code)
            jur_by_key[key] = {
                "authority_code": code,
                "area_kind": area_kind,
                "area_code": area_code,
            }

    # Drop jurisdictions whose authority was skipped. They can only exist if the authority
    # survived, but this keeps the invariant explicit.
    jurisdictions = [j for j in jur_by_key.values() if j["authority_code"] in by_code]
    return ParsedSeed(list(by_code.values()), jurisdictions)


def load_seed(path: Path | str = DEFAULT_SEED) -> ParsedSeed:
    """Read and parse the seed JSON file into authority and jurisdiction rows."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"seed {path} must be a JSON list, got {type(data).__name__}")
    return parse_seed(data)


_UPSERT_AUTHORITY_SQL = text(
    """
    INSERT INTO authority (
        code, authority_type, name, address, databox,
        source_uri, fetched_at, updated_at
    ) VALUES (
        :code, :authority_type, :name, :address, :databox,
        :source_uri, :fetched_at, now()
    )
    ON CONFLICT (code) DO UPDATE SET
        authority_type = EXCLUDED.authority_type,
        name           = EXCLUDED.name,
        address        = EXCLUDED.address,
        databox        = EXCLUDED.databox,
        source_uri     = EXCLUDED.source_uri,
        fetched_at     = EXCLUDED.fetched_at,
        updated_at     = now()
    """
)

_UPSERT_JURISDICTION_SQL = text(
    """
    INSERT INTO authority_jurisdiction (
        authority_code, area_kind, area_code, updated_at
    ) VALUES (
        :authority_code, :area_kind, :area_code, now()
    )
    ON CONFLICT (authority_code, area_kind, area_code) DO UPDATE SET
        updated_at = now()
    """
)


async def upsert_seed(parsed: ParsedSeed) -> tuple[int, int]:
    """Upsert authorities, then their jurisdictions, and return (n_auth, n_jur).

    Authorities go first so the jurisdiction foreign key is satisfied. Both run in a single
    transaction (session_scope commits on clean exit).
    """
    if not parsed.authorities and not parsed.jurisdictions:
        return (0, 0)
    async with session_scope() as session:
        if parsed.authorities:
            await session.execute(_UPSERT_AUTHORITY_SQL, parsed.authorities)
        if parsed.jurisdictions:
            await session.execute(_UPSERT_JURISDICTION_SQL, parsed.jurisdictions)
    return (len(parsed.authorities), len(parsed.jurisdictions))


async def run(path: Path | str = DEFAULT_SEED) -> tuple[int, int]:
    """Load the seed file, upsert it, and return (n_authorities, n_jurisdictions)."""
    parsed = load_seed(path)
    n_auth, n_jur = await upsert_seed(parsed)
    async with session_scope() as session:
        total_auth = (await session.execute(text("SELECT count(*) FROM authority"))).scalar_one()
        total_jur = (
            await session.execute(text("SELECT count(*) FROM authority_jurisdiction"))
        ).scalar_one()
    print(
        f"authority registry: upserted {n_auth} authorities / {n_jur} jurisdictions; "
        f"tables now have {total_auth} authorities / {total_jur} jurisdictions"
    )
    return (n_auth, n_jur)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the authority registry.")
    parser.add_argument(
        "--seed",
        default=str(DEFAULT_SEED),
        help="path to the authority registry JSON seed",
    )
    args = parser.parse_args()
    asyncio.run(run(args.seed))


if __name__ == "__main__":
    main()
