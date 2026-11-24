#!/usr/bin/env python3
"""Apply the ordered raw-SQL migration chain (006 to 014) to a regsuper database.

The project does not use Alembic: migrations are hand-written SQL files in `migrations/`, each
wrapped in its own `BEGIN; ... COMMIT;`. This driver applies the chain in strict order, one file at
a time, and refuses a database whose name does not contain 'regsuper'. `--dry-run` only parses
files: the staged 015 and 016 and, with `--include-chain`, also 006 to 014.

Usage (from app/): python scripts/apply_migrations.py [--database-url DSN] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

# ── Repo geometry ────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# The applied chain; 014 is the last applied migration. Order matters: 007 backfills
# 006's columns, 008, 009 and 010 build on 007, 011 on 009 and 010, 012 on 011, 013
# on 012 and 014 on 013.
APPLIED_CHAIN: tuple[str, ...] = (
    "006_tenancy.sql",
    "007_tenancy_backfill.sql",
    "008_rls_roles.sql",
    "009_plans_entitlements.sql",
    "010_profiles_advisor.sql",
    "011_runs_metering_billing.sql",
    "012_compliance.sql",
    "013_trust_audit_export.sql",
    "014_corpus_cutover.sql",
)

# Written but never applied by this driver; `--dry-run` parses them. Each is applied
# only after its own check passes: 015 (row-level security enforcement) after the
# cross-tenant leak test, 016 (the Conduit credential table) once Conduit's
# row-level security is in place. The local run scripts do apply 015 to the
# development database (they connect as `regsuper_app`). The files live under
# `migrations/staged/` because Postgres initdb mounts `migrations/` into
# `/docker-entrypoint-initdb.d` and does not recurse into subdirectories, so a clean
# `docker compose up` never applies them. migration_path() and read_sql() resolve
# the path prefixes.
STAGED: tuple[str, ...] = (
    "staged/015_rls_enforce.sql",
    "staged/016_conduit_credential.sql",
)

# Rollback files, kept under `migrations/rollback/` for the same initdb reason as
# STAGED, so a clean database never undoes its own schema. Not used by this driver;
# listed so the layout is documented in one place.
ROLLBACK: tuple[str, ...] = (
    "rollback/008_rls_roles_rollback.sql",
    "rollback/009_plans_entitlements_rollback.sql",
    "rollback/010_profiles_advisor_rollback.sql",
    "rollback/011_runs_metering_billing_rollback.sql",
)


# ── Target-DB guard ──────────────────────────────────────────────────────────
class NotATargetError(RuntimeError):
    """Raised when the DSN does not look like a regsuper target database."""


def _dbname_from_dsn(dsn: str) -> str:
    """Best-effort extract the database name (last path segment, sans query)."""
    tail = dsn.rsplit("/", 1)[-1]
    return tail.split("?", 1)[0]


def assert_is_target(dsn: str) -> None:
    """Refuse to run against a database that is not a regsuper target.

    A simple guard, not a security boundary: the database name must contain
    'regsuper' (which covers `regsuper`, `regsuper_scratch`, `regsuper_test`, a
    copy of prod and so on). It stops a stray environment variable from pointing
    the chain at a system database or another project's database.

    Export `APPLY_MIGRATIONS_ALLOW_ANY_DB=1` to allow a scratch database with a
    different name.
    """
    if os.environ.get("APPLY_MIGRATIONS_ALLOW_ANY_DB") == "1":
        return
    name = _dbname_from_dsn(dsn).lower()
    if "regsuper" not in name:
        raise NotATargetError(
            f"Refusing to run migrations against database {name!r}: name does "
            "not contain 'regsuper'. Point DATABASE_URL_SYNC at a regsuper "
            "target (or a regsuper-named scratch/copy DB). Set "
            "APPLY_MIGRATIONS_ALLOW_ANY_DB=1 to override for a scratch DB."
        )


# ── DSN handling ─────────────────────────────────────────────────────────────
def _resolve_sync_dsn(explicit: str | None) -> str:
    """Resolve the target DSN: the explicit flag, then DATABASE_URL_SYNC, then the config."""
    if explicit:
        return explicit
    env = os.environ.get("DATABASE_URL_SYNC")
    if env:
        return env
    # Fall back to the project's configured default (keeps one source of truth).
    try:
        from regsuper.config import get_settings

        return get_settings().database_url_sync
    except Exception:  # pragma: no cover - config import is best-effort
        return "postgresql://regsuper:regsuper@localhost:5432/regsuper"


def to_asyncpg_url(sync_dsn: str) -> str:
    """Translate a sync `postgresql://` DSN to the asyncpg async driver URL.

    asyncpg is the only Postgres driver installed, so we always go async. A DSN
    already carrying a `+driver` is normalised to `+asyncpg`.
    """
    if sync_dsn.startswith("postgresql+asyncpg://"):
        return sync_dsn
    if sync_dsn.startswith("postgresql+"):
        # Strip whatever +driver is present, re-attach asyncpg.
        rest = sync_dsn.split("://", 1)[1]
        return f"postgresql+asyncpg://{rest}"
    if sync_dsn.startswith("postgresql://"):
        return "postgresql+asyncpg://" + sync_dsn[len("postgresql://") :]
    if sync_dsn.startswith("postgres://"):
        return "postgresql+asyncpg://" + sync_dsn[len("postgres://") :]
    return sync_dsn


def _plain_dsn(sync_dsn: str) -> str:
    """A plain `postgresql://` DSN for `asyncpg.connect` (strip any `+driver`)."""
    scheme, sep, rest = sync_dsn.partition("://")
    if sep and "+" in scheme:
        return "postgresql://" + rest
    return sync_dsn


# ── File / SQL helpers ───────────────────────────────────────────────────────
def migration_path(name: str) -> Path:
    # `name` may carry a subdir prefix (e.g. "staged/015_rls_enforce.sql" or
    # "rollback/008_rls_roles_rollback.sql"); `Path / "a/b"` resolves correctly.
    return MIGRATIONS_DIR / name


def read_sql(name: str) -> str:
    p = migration_path(name)
    if not p.is_file():
        raise FileNotFoundError(f"migration file not found: {p}")
    return p.read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    """Remove `-- line comments` and `/* block comments */` for coarse parsing.

    Good enough for the structural checks here (statement count, BEGIN/COMMIT
    presence). Not a real SQL tokenizer: it does not understand `--` inside a
    string literal, which the migrations do not contain.
    """
    no_block = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    no_line = re.sub(r"--[^\n]*", "", no_block)
    return no_line


def dry_parse(name: str) -> dict:
    """Read and structurally validate a migration without applying it.

    Returns a small report dict. Raises on a structural problem (empty file, no
    statements). `--dry-run` runs this over the staged 015 and 016 (and optionally
    the 006-014 chain), so file validity is checked without touching a database.
    """
    raw = read_sql(name)
    body = _strip_sql_comments(raw).strip()
    if not body:
        raise ValueError(f"{name}: file is empty after stripping comments")
    statements = [s.strip() for s in body.split(";") if s.strip()]
    if not statements:
        raise ValueError(f"{name}: no SQL statements found")
    upper = body.upper()
    return {
        "name": name,
        "bytes": len(raw),
        "statements": len(statements),
        "has_begin": "BEGIN" in upper,
        "has_commit": "COMMIT" in upper,
    }


# ── Apply ────────────────────────────────────────────────────────────────────
async def _apply_one(conn, name: str) -> None:
    """Apply a single migration file through asyncpg's simple-query protocol.

    Each migration ships its own `BEGIN; … COMMIT;` wrapper, so the file is
    atomic on its own. It runs through the raw asyncpg connection's ``execute``
    (the simple protocol), not SQLAlchemy's normal path: SQLAlchemy prepares
    statements, and PostgreSQL rejects a multi-command batch in a prepared
    statement ("cannot insert multiple commands into a prepared statement"). The
    simple protocol also runs the dollar-quoted trigger-function bodies that a
    naive `;`-split would break.
    """
    await conn.execute(read_sql(name))


def _strip_outer_txn(sql: str) -> str:
    """Remove the leading `BEGIN;` and trailing `COMMIT;` so the driver owns the transaction.

    Only the outer wrapper is removed; nested savepoints, if any, are left intact. A
    migration that ships no wrapper is returned unchanged.
    """
    out = sql
    # Leading BEGIN; (only whitespace may precede it).
    out = re.sub(r"^\s*BEGIN\s*;\s*", "", out, count=1, flags=re.IGNORECASE)
    # Trailing COMMIT; (last occurrence).
    out = re.sub(r"\s*COMMIT\s*;\s*$", "\n", out, count=1, flags=re.IGNORECASE)
    return out


async def apply_chain(sync_dsn: str, chain: tuple[str, ...] = APPLIED_CHAIN) -> list[str]:
    """Apply the ordered chain to the target database and return the applied names.

    Uses a raw asyncpg connection (simple-query protocol) so each file's own
    multi-statement `BEGIN; … COMMIT;` batch applies atomically (see _apply_one);
    the chain stops at the first failure. This is an apply driver, not a tracker:
    there is no schema_migrations ledger. Idempotency comes from the migrations
    themselves (`CREATE … IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), and
    re-running a non-idempotent statement fails loudly, by design, so drift shows.
    """
    import asyncpg

    assert_is_target(sync_dsn)
    # Check that every file exists before touching the database, so a missing file
    # fails the whole run up front.
    for name in chain:
        if not migration_path(name).is_file():
            raise FileNotFoundError(f"migration file not found: {migration_path(name)}")

    conn = await asyncpg.connect(_plain_dsn(sync_dsn))
    applied: list[str] = []
    try:
        for name in chain:
            await _apply_one(conn, name)
            applied.append(name)
            print(f"  applied {name}")
    finally:
        await conn.close()
    return applied


# ── CLI ──────────────────────────────────────────────────────────────────────
def _run_dry_run(also_chain: bool) -> int:
    targets = list(STAGED)
    if also_chain:
        targets = list(APPLIED_CHAIN) + targets
    print("DRY RUN — parsing migration files (no DB writes):")
    ok = True
    for name in targets:
        try:
            rep = dry_parse(name)
            tag = "STAGED" if name in STAGED else "chain"
            print(
                f"  [{tag}] {name}: {rep['statements']} statements, "
                f"{rep['bytes']} bytes, BEGIN={rep['has_begin']} COMMIT={rep['has_commit']}"
            )
        except (FileNotFoundError, ValueError) as exc:
            ok = False
            print(f"  FAIL {name}: {exc}")
    if not ok:
        print("dry-run: one or more files failed to parse", file=sys.stderr)
        return 1
    print("dry-run: all parsed OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="Target sync DSN (postgresql://…). Defaults to DATABASE_URL_SYNC.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse the FILE-STAGED 015/016 (and, with --include-chain, the "
        "006-014 set) without applying anything.",
    )
    parser.add_argument(
        "--include-chain",
        action="store_true",
        help="With --dry-run, also parse the 006-014 applied chain.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        return _run_dry_run(also_chain=args.include_chain)

    sync_dsn = _resolve_sync_dsn(args.database_url)
    try:
        assert_is_target(sync_dsn)
    except NotATargetError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"Applying {len(APPLIED_CHAIN)} migrations (006→014) to {_dbname_from_dsn(sync_dsn)!r}:")
    applied = asyncio.run(apply_chain(sync_dsn))
    print(
        f"Done — {len(applied)} migrations applied. 015/016 are FILE-STAGED and were NOT applied."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
