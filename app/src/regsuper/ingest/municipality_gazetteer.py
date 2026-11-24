"""Load the official Czech municipality gazetteer into the `municipality` table.

Each municipality (obec) is linked to its district (okres), ORP and region (kraj). The seed,
``data/geo/municipality_gazetteer.json``, is a join of ČSÚ code list 43 (CISOB) with its links
to lists 65 (SO ORP), 101 (okres) and 100 (kraj); the raw sources are in ``data/geo/*.csv``.
Every run upserts each seed row on the `code` primary key, so re-runs never duplicate rows and
always converge the table to the seed.

Usage (from app/, with secrets.env loaded):
    .venv/bin/python -m regsuper.ingest.municipality_gazetteer [--seed PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from regsuper.storage.database import session_scope

# app/data/geo/municipality_gazetteer.json: this file lives in src/regsuper/ingest/, so the
# fourth parent directory is the app root.
DEFAULT_SEED = Path(__file__).resolve().parents[3] / "data" / "geo" / "municipality_gazetteer.json"

# The gazetteer columns, in the fixed order the upsert binds against.
_COLUMNS = (
    "code",
    "name",
    "okres_code",
    "okres_name",
    "orp_code",
    "orp_name",
    "kraj_code",
    "kraj_name",
    "okres_csu_code",
    "kraj_csu_code",
)


def _clean(value: Any) -> str | None:
    """Normalise a cell to a stripped string, or None when empty; non-strings are stringified."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def parse_rows(raw: list[dict[str, Any]]) -> list[dict[str, str | None]]:
    """Normalise seed dicts into clean gazetteer rows without touching the database.

    Rows missing `code` or `name` are skipped, since `code` is the primary key and `name` is
    NOT NULL. Every other column becomes a trimmed string or None. Rows are de-duplicated on
    `code` (last one wins), so the output can go straight into the primary-key upsert.
    """
    by_code: dict[str, dict[str, str | None]] = {}
    for item in raw:
        code = _clean(item.get("code"))
        name = _clean(item.get("name"))
        if not code or not name:
            continue
        by_code[code] = {col: _clean(item.get(col)) for col in _COLUMNS}
    return list(by_code.values())


def load_seed(path: Path | str = DEFAULT_SEED) -> list[dict[str, str | None]]:
    """Read and parse the seed JSON file into clean rows."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"seed {path} must be a JSON list, got {type(data).__name__}")
    return parse_rows(data)


_UPSERT_SQL = text(
    """
    INSERT INTO municipality (
        code, name, okres_code, okres_name, orp_code, orp_name,
        kraj_code, kraj_name, okres_csu_code, kraj_csu_code, updated_at
    ) VALUES (
        :code, :name, :okres_code, :okres_name, :orp_code, :orp_name,
        :kraj_code, :kraj_name, :okres_csu_code, :kraj_csu_code, now()
    )
    ON CONFLICT (code) DO UPDATE SET
        name           = EXCLUDED.name,
        okres_code     = EXCLUDED.okres_code,
        okres_name     = EXCLUDED.okres_name,
        orp_code       = EXCLUDED.orp_code,
        orp_name       = EXCLUDED.orp_name,
        kraj_code      = EXCLUDED.kraj_code,
        kraj_name      = EXCLUDED.kraj_name,
        okres_csu_code = EXCLUDED.okres_csu_code,
        kraj_csu_code  = EXCLUDED.kraj_csu_code,
        updated_at     = now()
    """
)


async def upsert_rows(rows: list[dict[str, str | None]]) -> int:
    """Upsert the given rows into `municipality` and return the number attempted.

    Runs in a single transaction (session_scope commits on clean exit).
    """
    if not rows:
        return 0
    async with session_scope() as session:
        await session.execute(_UPSERT_SQL, rows)
    return len(rows)


async def run(path: Path | str = DEFAULT_SEED) -> int:
    """Load the seed file, upsert it, and return the number of rows loaded."""
    rows = load_seed(path)
    n = await upsert_rows(rows)
    async with session_scope() as session:
        total = (await session.execute(text("SELECT count(*) FROM municipality"))).scalar_one()
    print(f"municipality gazetteer: upserted {n} rows; table now has {total} rows")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the municipality gazetteer.")
    parser.add_argument(
        "--seed",
        default=str(DEFAULT_SEED),
        help="path to the joined gazetteer JSON seed",
    )
    args = parser.parse_args()
    asyncio.run(run(args.seed))


if __name__ == "__main__":
    main()
