"""Seed the competent-authority tables from the draft fixtures (a one-off operator step).

Loads the draft offices into `authority`, their area-to-office rows into
`authority_jurisdiction` and the topic-to-authority-type map into `topic_authority`, so the
resolver that answers "where to file, which office" (kam podat, který úřad) has data. Nothing
runs on import; `--dry-run` parses the fixtures and prints row counts without touching the database.

Usage (from app/, with secrets.env loaded and migration 022 applied):
    .venv/bin/python -m regsuper.ingest.seed_authorities [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from regsuper.analysis import authority_vocab as vocab
from regsuper.ingest.authority_registry import ParsedSeed, load_seed
from regsuper.storage.database import session_scope

# Why this is separate from authority_registry.py: that module is the general directory refresh,
# an upsert that rewrites every column on each run. This one is a first-time, insert-only load
# (ON CONFLICT DO NOTHING) that never overwrites a row an operator or a later real-directory load
# already placed, so the draft provenance (source_uri, fetched_at) stays exactly as seeded.
# Re-running is a no-op for codes already present.
#
# The serving code reads the topic-to-authority-type map from a JSON file (the
# authority_topic_map_path setting, via authority_resolver); migration 022 creates only
# `authority` and `authority_jurisdiction`. Topic rows are therefore written only when a
# `topic_authority` table exists, and the map file stays the resolver's source of truth.
#
# The draft fixtures await review by a Czech advocate (ČAK member), who must validate the seed
# and the topic map before they drive customer-facing "where to file" answers. They are the same
# files the resolver settings (config.authority_*_path) default to, so the database and the
# resolver read one and the same seed.
_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "authority"
DEFAULT_AUTHORITY_SEED = _FIXTURE_DIR / "authority_registry.sample.json"
DEFAULT_TOPIC_MAP = _FIXTURE_DIR / "topic_authority.sample.json"


# Insert-only statements. Unlike authority_registry.py's DO UPDATE, the seed must not overwrite
# a row that is already present, so its source_uri and fetched_at stay put.
_SEED_AUTHORITY_SQL = text(
    """
    INSERT INTO authority (
        code, authority_type, name, address, databox,
        source_uri, fetched_at, updated_at
    ) VALUES (
        :code, :authority_type, :name, :address, :databox,
        :source_uri, :fetched_at, now()
    )
    ON CONFLICT (code) DO NOTHING
    """
)

_SEED_JURISDICTION_SQL = text(
    """
    INSERT INTO authority_jurisdiction (
        authority_code, area_kind, area_code, updated_at
    ) VALUES (
        :authority_code, :area_kind, :area_code, now()
    )
    ON CONFLICT (authority_code, area_kind, area_code) DO NOTHING
    """
)

_SEED_TOPIC_AUTHORITY_SQL = text(
    """
    INSERT INTO topic_authority (topic_key, authority_type, priority)
    VALUES (:topic_key, :authority_type, :priority)
    ON CONFLICT (topic_key, authority_type) DO NOTHING
    """
)


def parse_topic_authority_rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a ``{"topics": {topic_key: [type, …]}}`` map into topic rows.

    Pure and database-free. Each ``(topic_key, authority_type, priority)`` row uses the
    position in the list as ``priority`` (0 is the most directly competent), matching the
    fixture's rule that order within a list is priority. Authority types are normalised through
    the shared vocabulary (``normalize_tag``); an unknown type is dropped so a typo can never
    seed a row that joins to nothing. Duplicate (topic, type) pairs collapse to their first
    occurrence. Never raises.
    """
    topics = raw.get("topics") if isinstance(raw, dict) else None
    if not isinstance(topics, dict):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for topic_key, types in topics.items():
        if not isinstance(topic_key, str) or not topic_key.strip():
            continue
        if not isinstance(types, (list, tuple)):
            continue
        priority = 0
        for t in types:
            tag = vocab.normalize_tag(t if isinstance(t, str) else None)
            if not tag:
                continue
            key = (topic_key.strip(), tag)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "topic_key": topic_key.strip(),
                    "authority_type": tag,
                    "priority": priority,
                }
            )
            priority += 1
    return rows


def load_topic_authority_rows(path: Path | str = DEFAULT_TOPIC_MAP) -> list[dict[str, Any]]:
    """Read and parse the topic-to-authority-type map JSON into flat topic rows."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_topic_authority_rows(data)


async def _table_exists(session: Any, name: str) -> bool:
    """Return True if ``name`` is an existing relation, using a regclass probe.

    Lets the topic seed do nothing on a database that has only migration 022 and no
    topic_authority table.
    """
    got = (await session.execute(text("SELECT to_regclass(:n)"), {"n": name})).scalar_one()
    return got is not None


def _as_date(v: Any) -> date | None:
    """Coerce a fixture ``fetched_at`` ISO date string into a ``date``.

    asyncpg binds a DATE column from a date or datetime object, not a string. Dates and None
    pass through unchanged; a malformed value becomes None.
    """
    if v is None or (isinstance(v, date) and not isinstance(v, datetime)):
        return v
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


async def upsert_authorities(parsed: ParsedSeed) -> tuple[int, int]:
    """Insert absent authorities, then their jurisdictions, and return (n_auth, n_jur).

    Re-running never rewrites a row that is already present. Authorities go first so the
    jurisdiction foreign key is satisfied; both run in one ``session_scope`` transaction.
    """
    if not parsed.authorities and not parsed.jurisdictions:
        return (0, 0)
    async with session_scope() as session:
        if parsed.authorities:
            # asyncpg binds the DATE column from a date object, not the fixture's ISO string.
            _auth_rows = [
                {**r, "fetched_at": _as_date(r.get("fetched_at"))} for r in parsed.authorities
            ]
            await session.execute(_SEED_AUTHORITY_SQL, _auth_rows)
        if parsed.jurisdictions:
            await session.execute(_SEED_JURISDICTION_SQL, parsed.jurisdictions)
    return (len(parsed.authorities), len(parsed.jurisdictions))


async def upsert_topic_authority(rows: list[dict[str, Any]]) -> int:
    """Insert absent topic-to-authority rows and return the number presented.

    If the ``topic_authority`` table does not exist (a database with only migration 022), this
    does nothing and returns 0 instead of raising; the resolver still reads its map from
    ``authority_topic_map_path``.
    """
    if not rows:
        return 0
    async with session_scope() as session:
        if not await _table_exists(session, "topic_authority"):
            return 0
        await session.execute(_SEED_TOPIC_AUTHORITY_SQL, rows)
    return len(rows)


async def run(
    authority_seed: Path | str = DEFAULT_AUTHORITY_SEED,
    topic_map: Path | str = DEFAULT_TOPIC_MAP,
) -> tuple[int, int, int]:
    """Seed authorities, jurisdictions and topic rows from the draft fixtures.

    Returns ``(n_authorities, n_jurisdictions, n_topic_rows)``. Idempotent: a second run seeds
    nothing new.
    """
    parsed = load_seed(authority_seed)
    n_auth, n_jur = await upsert_authorities(parsed)
    topic_rows = load_topic_authority_rows(topic_map)
    n_topic = await upsert_topic_authority(topic_rows)
    print(
        f"authority seed: {n_auth} DRAFT authorities / {n_jur} jurisdictions / "
        f"{n_topic} topic rows presented (ON CONFLICT DO NOTHING — provenance preserved)"
    )
    return (n_auth, n_jur, n_topic)


def dry_run(
    authority_seed: Path | str = DEFAULT_AUTHORITY_SEED,
    topic_map: Path | str = DEFAULT_TOPIC_MAP,
) -> tuple[int, int, int]:
    """Parse the fixtures and report the row counts without touching the database."""
    parsed = load_seed(authority_seed)
    topic_rows = load_topic_authority_rows(topic_map)
    n = (len(parsed.authorities), len(parsed.jurisdictions), len(topic_rows))
    print(
        f"[dry-run] would seed {n[0]} authorities / {n[1]} jurisdictions / "
        f"{n[2]} topic rows (no DB write)"
    )
    return n


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the DRAFT competent-authority registry + topic map (ops-76)."
    )
    parser.add_argument(
        "--authority-seed",
        default=str(DEFAULT_AUTHORITY_SEED),
        help="path to the DRAFT authority registry JSON (parse_seed shape)",
    )
    parser.add_argument(
        "--topic-map",
        default=str(DEFAULT_TOPIC_MAP),
        help="path to the DRAFT topic→authority-type map JSON",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + report counts without writing the DB",
    )
    args = parser.parse_args()
    if args.dry_run:
        dry_run(args.authority_seed, args.topic_map)
        return
    asyncio.run(run(args.authority_seed, args.topic_map))


if __name__ == "__main__":
    main()
