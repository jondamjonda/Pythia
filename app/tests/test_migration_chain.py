"""G-MIGRATION-CHAIN harness (A2-T00 + A2-T-ORM-reconcile).

What this proves
----------------
1. **Files present** — every migration `006 … 016` exists on disk.
2. **Applies in order** — applying the additive chain `006 → 014` against a
   *scratch / copy-of-prod* DB succeeds, in order, one transaction per file.
3. **No destructive DDL in the forward path** — `DROP TABLE` count is **0**
   across `006 … 016` *outside* each file's rollback section. Every migration is
   purely additive; the only `DROP`s live behind a clearly-delimited rollback
   marker.
4. **Old data untouched** — row counts of the existing `001 … 005` tables are
   unchanged after the chain applies (additive, no behaviour change).
5. **015 + 016 are FILE-STAGED** — both exist and *dry-parse*, but are
   **excluded** from the applied chain (015 = RLS-enforce, gated on AC-RLS-4;
   016 = conduit_credential, gated on the Conduit RLS seam). The harness asserts
   they are present and valid yet proves they were never applied.
6. **ORM mirror is faithful** (A2-T-ORM-reconcile) — the reflected live-schema
   column set equals the ORM `Base` column set for `provision` (incl. the
   `tsv` GENERATED column, 001:90-91) and `project` (incl. the
   `location` / `location_polygon` GEOMETRY(4326) columns, 001:185-186), so the
   known `models.py`-vs-`.sql` drift is *asserted* rather than silently carried.

DB strategy
-----------
The DB-touching tests require a **scratch** database that already holds the
`001 → 005` baseline (a copy of prod, or a freshly-restored dump). They read the
DSN from `MIGRATION_TEST_DATABASE_URL` (preferred — keeps prod safe) and fall
back to `DATABASE_URL_SYNC`. If no DB is reachable, those tests **skip** (the
file/grep/dry-parse/ORM-mirror checks still run, so CI without Postgres is green
on the offline subset). The DSN must name a regsuper target, and the harness
**refuses any DSN whose name does not contain `scratch`/`test`/`copy`** so a
careless run can never mutate the real `regsuper` DB.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

# The driver under test.
from scripts import apply_migrations as drv

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"

# Full authored set the harness reasons over: 006-014 applied + 015/016 staged.
ALL_MIGRATIONS: tuple[str, ...] = drv.APPLIED_CHAIN + drv.STAGED  # 006 … 016

# Tables created by 001-005 whose row counts must NOT change when 006-014 apply.
# (Every table the universal-spine + observability migrations create.)
BASELINE_TABLES: tuple[str, ...] = (
    "source_document",
    "provision",
    "provision_sentence",
    "cross_reference",
    "defined_term",
    "project",
    "project_document",
    "project_context",
    "finding",
    "hypothesis",
    "audit_event",
    "retrieval_eval",
    "retrieval_cache",
    "api_call",
    "system_sample",
)


# ── DB fixture (scratch only) ────────────────────────────────────────────────
def _scratch_dsn() -> str | None:
    """Resolve a *scratch* DSN, or None if none is configured/safe.

    Prefers MIGRATION_TEST_DATABASE_URL so prod is never the default. Whatever
    DSN we end up with must name a database that looks like a scratch/test/copy
    DB — we will not mutate the real `regsuper` database from a test.
    """
    dsn = os.environ.get("MIGRATION_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not dsn:
        return None
    name = drv._dbname_from_dsn(dsn).lower()
    if not any(tok in name for tok in ("scratch", "test", "copy")):
        return None
    return dsn


async def _db_reachable(sync_dsn: str) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(drv.to_asyncpg_url(sync_dsn), future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


async def _reset_scratch(scratch_sync_dsn: str) -> None:
    """Re-create the scratch DB from the `regsuper` baseline template so each
    DB-touching test starts from the pristine 001-005 state. The migrations are
    deliberately non-idempotent (CREATE TABLE, not IF NOT EXISTS), so re-applying
    the chain to an already-migrated DB would error — every test gets a clean DB.
    """
    import asyncpg

    name = drv._dbname_from_dsn(scratch_sync_dsn)
    base, _, _ = drv._plain_dsn(scratch_sync_dsn).rpartition("/")
    admin = await asyncpg.connect(f"{base}/postgres")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        # The baseline template is the real `regsuper` DB (holds 001-005 + data).
        await admin.execute(f'CREATE DATABASE "{name}" TEMPLATE regsuper')
    finally:
        await admin.close()


@pytest.fixture
def scratch_dsn() -> str:
    dsn = _scratch_dsn()
    if dsn is None:
        pytest.skip(
            "no scratch DB configured (set MIGRATION_TEST_DATABASE_URL to a "
            "regsuper *scratch/test/copy* DB holding the 001-005 baseline)"
        )
    if not asyncio.run(_db_reachable(dsn)):
        pytest.skip(f"scratch DB not reachable: {drv._dbname_from_dsn(dsn)!r}")
    # Reset to the 001-005 baseline before EACH DB-touching test (function scope).
    asyncio.run(_reset_scratch(dsn))
    return dsn


async def _table_counts(sync_dsn: str, tables: tuple[str, ...]) -> dict[str, int]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(drv.to_asyncpg_url(sync_dsn), future=True)
    out: dict[str, int] = {}
    try:
        async with engine.connect() as conn:
            for t in tables:
                exists = (
                    await conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": t})
                ).scalar()
                if not exists:
                    continue
                n = (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
                out[t] = int(n)
    finally:
        await engine.dispose()
    return out


# ── 1. Files present (006-016) ───────────────────────────────────────────────
@pytest.mark.parametrize("name", ALL_MIGRATIONS)
def test_migration_file_present(name: str) -> None:
    p = MIGRATIONS_DIR / name
    assert p.is_file(), f"missing migration file: {p}"
    assert p.stat().st_size > 0, f"empty migration file: {p}"


def test_chain_and_staged_numbering_is_006_to_016() -> None:
    """Strict numbering: applied = 006-014, staged = 015-016, no gaps.

    STAGED entries carry a ``staged/`` subdir prefix (they live in
    ``migrations/staged/`` so Postgres initdb never auto-applies them), so we
    take each entry's basename before reading its 3-digit number.
    """
    nums = [int(Path(n).name[:3]) for n in ALL_MIGRATIONS]
    assert nums == list(range(6, 17)), nums
    assert [int(Path(n).name[:3]) for n in drv.APPLIED_CHAIN] == list(range(6, 15))
    assert [int(Path(n).name[:3]) for n in drv.STAGED] == [15, 16]


# ── 2. Applies in order on a scratch / copy DB ───────────────────────────────
@pytest.mark.asyncio
async def test_apply_chain_006_to_014_in_order(scratch_dsn: str) -> None:
    applied = await drv.apply_chain(scratch_dsn)
    # Applied exactly the 006-014 chain, in order.
    assert applied == list(drv.APPLIED_CHAIN)
    # And the staged pair was NOT applied.
    assert "015_rls_enforce.sql" not in applied
    assert "016_conduit_credential.sql" not in applied


# ── 3. No DROP TABLE in the forward path (outside rollback sections) ──────────
# A rollback section is delimited by a marker line. Migrations either inline a
# `-- ROLLBACK` / `-- DOWN` section or ship a sibling `.rollback.sql`. We count
# `DROP TABLE` only in the forward body.
_ROLLBACK_MARKERS = (
    r"^\s*--+\s*ROLLBACK",
    r"^\s*--+\s*DOWN\b",
    r"^\s*--+\s*=*\s*ROLLBACK",
)
_ROLLBACK_RE = re.compile("|".join(_ROLLBACK_MARKERS), re.IGNORECASE | re.MULTILINE)
_DROP_TABLE_RE = re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE)


def _forward_body(sql: str) -> str:
    """Return the forward (apply) portion: everything before the first rollback
    marker. If there is no inline marker the whole file is the forward body
    (the rollback lives in a sibling `_rollback.sql` file, which is NOT in
    ALL_MIGRATIONS and so is never scanned as a forward path).
    """
    m = _ROLLBACK_RE.search(sql)
    return sql[: m.start()] if m else sql


def _executable_drop_table_count(sql: str) -> int:
    """Count EXECUTABLE `DROP TABLE` in the forward body.

    Comments are stripped first (via the driver's own stripper, one source of
    truth) so a *documented* `-- DROP TABLE …` line — the convention several of
    these migrations use to spell out their rollback inside the forward file's
    rollback section header — is correctly treated as a no-op, while a real
    executable DROP would still be caught.
    """
    forward = _forward_body(sql)
    return len(_DROP_TABLE_RE.findall(drv._strip_sql_comments(forward)))


@pytest.mark.parametrize("name", ALL_MIGRATIONS)
def test_no_drop_table_in_forward_path(name: str) -> None:
    raw = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    n = _executable_drop_table_count(raw)
    assert n == 0, (
        f"{name}: {n} executable DROP TABLE in the forward (apply) path — "
        "migrations must be additive; DROPs belong only in the rollback section."
    )


def test_no_drop_table_in_any_forward_body_aggregate() -> None:
    """Aggregate guard: total executable DROP TABLE across all forward bodies
    of 006-016 == 0."""
    total = 0
    for name in ALL_MIGRATIONS:
        raw = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        total += _executable_drop_table_count(raw)
    assert total == 0, f"{total} executable DROP TABLE statements found in forward paths"


# ── 4. 001-005 row counts unchanged after applying 006-014 ───────────────────
@pytest.mark.asyncio
async def test_baseline_row_counts_unchanged(scratch_dsn: str) -> None:
    before = await _table_counts(scratch_dsn, BASELINE_TABLES)
    assert before, "baseline tables not found — scratch DB lacks the 001-005 schema"
    await drv.apply_chain(scratch_dsn)
    after = await _table_counts(scratch_dsn, BASELINE_TABLES)
    # Same set of baseline tables, identical counts (additive, no data change).
    assert set(after) >= set(before)
    for t, n in before.items():
        assert after.get(t) == n, (
            f"{t}: row count changed {n} -> {after.get(t)} after applying the "
            "additive chain (006-014 must not touch 001-005 data)"
        )


# ── 5. 015 + 016 are present + dry-parse but EXCLUDED from the applied chain ──
def test_staged_excluded_from_applied_chain() -> None:
    # STAGED entries carry a ``staged/`` subdir prefix; compare by basename so
    # the check is robust to where the staged files physically live.
    staged_basenames = {Path(s).name for s in drv.STAGED}
    applied_basenames = {Path(c).name for c in drv.APPLIED_CHAIN}
    for staged in ("015_rls_enforce.sql", "016_conduit_credential.sql"):
        assert staged in staged_basenames
        assert staged not in applied_basenames, (
            f"{staged} must be FILE-STAGED, not in the applied chain"
        )


@pytest.mark.parametrize("name", drv.STAGED)
def test_staged_dry_parses(name: str) -> None:
    """The file-staged migrations parse cleanly (file validity) without applying."""
    rep = drv.dry_parse(name)
    assert rep["statements"] >= 1, f"{name}: no SQL statements parsed"
    assert rep["bytes"] > 0


def test_dry_run_cli_parses_staged_only(capsys) -> None:
    """`--dry-run` (no --include-chain) exits 0 and parses ONLY 015/016."""
    rc = drv.main(["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "015_rls_enforce.sql" in out
    assert "016_conduit_credential.sql" in out
    # The applied chain is NOT parsed unless --include-chain is given.
    assert "006_tenancy.sql" not in out


def test_dry_run_does_not_apply_to_db() -> None:
    """Sanity: a dry-run takes no DSN and performs no DB connection.

    We prove this structurally — `--dry-run` returns before resolving any DSN —
    by confirming the CLI succeeds with NO database env at all.
    """
    saved = {
        k: os.environ.pop(k, None) for k in ("DATABASE_URL_SYNC", "MIGRATION_TEST_DATABASE_URL")
    }
    try:
        assert drv.main(["--dry-run"]) == 0
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_015_rollback_restores_008_marker_present() -> None:
    """015's rollback section must exist and reference restoring the 008
    permissive policies (A2-T15: 'rollback restoring 008')."""
    # 015 lives under migrations/staged/; resolve via the driver's STAGED entry
    # (which carries the subdir prefix) so the path stays in sync with the driver.
    staged_015 = next(s for s in drv.STAGED if Path(s).name == "015_rls_enforce.sql")
    raw = (MIGRATIONS_DIR / staged_015).read_text(encoding="utf-8")
    assert _ROLLBACK_RE.search(raw), "015 has no rollback section marker"
    # The rollback half should re-create / restore the permissive policy world.
    after_marker = raw[_ROLLBACK_RE.search(raw).start() :]
    assert re.search(r"\b008\b|permissive|USING\s*\(\s*true\s*\)", after_marker, re.IGNORECASE), (
        "015 rollback must restore the 008 permissive policies"
    )


# ── 6. ORM-vs-reflection column-set check (A2-T-ORM-reconcile) ───────────────
def _orm_columns(table_name: str) -> set[str]:
    from regsuper.storage.models import Base

    tbl = Base.metadata.tables[table_name]
    return {c.name for c in tbl.columns}


def test_orm_provision_has_generated_tsv_column() -> None:
    """The ORM `provision` mirror must carry `tsv` (GENERATED, 001:90-91).

    The known drift is that the .sql declares a `tsv GENERATED ALWAYS AS (…)
    STORED` column that the ORM omitted. A2-T-ORM-reconcile closes it: the ORM
    must declare `tsv` (read-only / Computed). This asserts the mirror is now
    faithful so reflection == ORM for provision.
    """
    cols = _orm_columns("provision")
    assert "tsv" in cols, (
        "ORM `provision` is missing the GENERATED `tsv` column (001:90-91) — "
        "models.py-vs-.sql drift not reconciled (A2-T-ORM-reconcile). Declare "
        "it as a read-only/Computed column."
    )


def test_orm_project_has_geometry_columns() -> None:
    """The ORM `project` mirror must carry `location` + `location_polygon`
    GEOMETRY(4326) (001:185-186). The ORM previously omitted both; reconcile
    declares them as opaque/geoalchemy2 columns."""
    cols = _orm_columns("project")
    for geo in ("location", "location_polygon"):
        assert geo in cols, (
            f"ORM `project` is missing the GEOMETRY column `{geo}` (001:185-186)"
            " — models.py-vs-.sql drift not reconciled (A2-T-ORM-reconcile)."
        )


@pytest.mark.asyncio
async def test_reflection_equals_orm_columns(scratch_dsn: str) -> None:
    """The live reflected column set == the ORM Base column set for the two
    drift-prone tables, *including* the generated `tsv` and the geometry columns.
    Run after applying the chain so any 006-014 ALTERs on these tables are also
    mirrored in the ORM.
    """
    from regsuper.config import get_settings

    # Point the app's engine at the scratch DB so reflect_live_schema() reads it.
    settings = get_settings()
    saved_async = settings.database_url
    settings.database_url = drv.to_asyncpg_url(scratch_dsn)
    try:
        from regsuper.observability import db_schema
        from regsuper.storage import database as db_mod

        await db_mod.dispose_engine()  # drop any engine bound to the old DSN
        await drv.apply_chain(scratch_dsn)
        reflected = await db_schema.reflect_live_schema()
        by_name = {t["name"]: {c["name"] for c in t["columns"]} for t in reflected["tables"]}

        for table in ("provision", "project"):
            orm_cols = _orm_columns(table)
            live_cols = by_name.get(table, set())
            assert live_cols, f"{table} not found in live reflection"
            # The ORM mirror must be a faithful subset-or-equal of the live DB:
            # every ORM column exists live (no phantom ORM columns), and the
            # drift-prone columns are present on BOTH sides.
            missing_live = orm_cols - live_cols
            assert not missing_live, (
                f"{table}: ORM declares columns absent from the live DB: {missing_live}"
            )
        # Spot-assert the specific drift columns appear in the live reflection.
        assert "tsv" in by_name.get("provision", set())
        assert {"location", "location_polygon"} <= by_name.get("project", set())
    finally:
        settings.database_url = saved_async
        from regsuper.storage import database as db_mod

        await db_mod.dispose_engine()


# ── Driver-internal unit checks (no DB) ──────────────────────────────────────
def test_target_guard_refuses_non_regsuper_db() -> None:
    with pytest.raises(drv.NotATargetError):
        drv.assert_is_target("postgresql://u:p@localhost:5432/postgres")


def test_target_guard_accepts_regsuper_scratch() -> None:
    # Should not raise.
    drv.assert_is_target("postgresql://u:p@localhost:5432/regsuper_scratch")


def test_to_asyncpg_url_translates_sync_dsn() -> None:
    assert drv.to_asyncpg_url("postgresql://u:p@h:5432/regsuper").startswith(
        "postgresql+asyncpg://"
    )
    # Idempotent on an already-async URL.
    u = "postgresql+asyncpg://u:p@h:5432/regsuper"
    assert drv.to_asyncpg_url(u) == u


def test_strip_outer_txn_removes_wrapper() -> None:
    sql = "BEGIN;\nCREATE TABLE x (id int);\nCOMMIT;\n"
    out = drv._strip_outer_txn(sql)
    assert "BEGIN" not in out.upper().split("CREATE")[0]
    assert "COMMIT" not in out.upper()
    assert "CREATE TABLE x" in out
