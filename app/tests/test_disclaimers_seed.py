"""B2b-01 (WP-032) — disclaimers registry ↔ 012 ``disclaimer`` table bijection.

Two layers:

1. **Pure registry** (always runs, no DB) — ``list_disclaimer_ids`` mirrors the
   in-code ``DISCLAIMERS`` registry, every block carries non-empty CZ+EN, and the
   answer_mode/confidence values stay inside the closed vocabularies. This pins
   the A3-04 contract and the "single source of truth for ids" claim.

2. **DB seed bijection + idempotency** (skips if no DB) — ``seed_disclaimers``
   UPSERTs each registry entry into the live ``disclaimer`` table; we prove the
   bijection (every registry id seeds exactly one row, the table holds no extra
   id) and idempotency (running it twice leaves byte-identical rows). The whole
   probe runs inside a single transaction that is **ALWAYS rolled back**, so the
   live ``disclaimer`` table is never mutated by the test.
"""

from __future__ import annotations

import asyncio

import pytest

from regsuper.analysis import disclaimers as d


# ── 1. Pure registry contract (no DB) ────────────────────────────────────────
def test_list_disclaimer_ids_mirrors_registry() -> None:
    ids = d.list_disclaimer_ids()
    # Deterministic list == registry keys, no dupes.
    assert ids == list(d.DISCLAIMERS.keys())
    assert len(ids) == len(set(ids)) == len(d.DISCLAIMERS)
    # The three stable id constants are exactly the registered set.
    assert set(ids) == {
        d.DISCLAIMER_INFO,
        d.DISCLAIMER_INFO_LOW_CONFIDENCE,
        d.DISCLAIMER_RESERVED_ADVICE,
    }


def test_every_block_has_nonempty_cs_en_and_valid_vocab() -> None:
    for did in d.list_disclaimer_ids():
        block = d.get_disclaimer(did)
        assert block.id == did, "registry key must equal block.id (PK identity)"
        assert block.text_cs.strip(), f"{did}: empty CZ text"
        assert block.text_en.strip(), f"{did}: empty EN text"
        assert block.answer_mode in ("information", "reserved_advice", "needs_review")
        assert block.confidence in ("normal", "low")


def test_import_has_no_db_side_effect() -> None:
    """seed is NOT invoked at import — importing the module touches no DB.

    Proven by a FRESH SUBPROCESS import (a clean interpreter, no DB env): if the
    module seeded at import it would need a live DB / raise, so a clean exit is the
    proof. We deliberately do NOT use ``importlib.reload`` here — reloading
    re-executes the module in place, rebinding its classes (e.g. ``DisclaimerBlock``)
    to fresh objects that leak into the rest of the session and break every later
    ``isinstance`` check against the registry types (it silently poisoned
    ``test_render``). The subprocess proof is both stronger and leak-free.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import regsuper.analysis.disclaimers"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"import raised (DB side effect at import?):\n{proc.stderr}"
    # And the seed entrypoint is an explicit, un-invoked coroutine (caller must await it).
    assert callable(d.seed_disclaimers)
    assert asyncio.iscoroutinefunction(d.seed_disclaimers)


# ── 2. DB bijection + idempotency (rollback-only; skips without a DB) ─────────
async def _db_reachable() -> bool:
    from sqlalchemy import text

    from regsuper.storage.database import dispose_engine, get_session_factory

    # The module-level engine/pool is a singleton bound to whichever event loop
    # first created it; pytest-asyncio runs each test in its OWN loop, so a pool
    # connection from a previous DB test would be on a dead loop here. Dispose
    # first so this test gets a fresh engine on the current loop (the same
    # dispose-between-DB-tests pattern test_migration_chain.py uses).
    await dispose_engine()
    try:
        factory = get_session_factory()
        async with factory() as s:
            ok = (await s.execute(text("SELECT to_regclass('disclaimer') IS NOT NULL"))).scalar()
        return bool(ok)
    except Exception:
        return False


async def _seed_and_read_in_rolled_back_txn() -> tuple[dict[str, dict], dict[str, dict], int]:
    """Seed twice inside ONE transaction, snapshot the rows after each pass, then
    roll the whole transaction back so the live table is never mutated.

    Returns (rows_after_first_seed, rows_after_second_seed, return_count_first).
    """
    from sqlalchemy import text

    from regsuper.storage.database import get_session_factory

    factory = get_session_factory()

    def _snapshot_sql() -> str:
        return (
            "SELECT id, answer_mode, confidence, text_cs, text_en, is_active "
            "FROM disclaimer ORDER BY id"
        )

    async with factory() as session:
        # Explicit transaction we will ROLL BACK no matter what.
        trans = await session.begin()
        try:
            # Guard: the probe only makes sense on an empty table (the live
            # default). If a real seed already populated it, skip the mutation.
            existing = (await session.execute(text("SELECT count(*) FROM disclaimer"))).scalar()
            if int(existing) != 0:
                pytest.skip("disclaimer table already populated; skipping mutating probe")

            n_first = await d.seed_disclaimers(session)
            first_rows = {
                r[0]: {
                    "answer_mode": r[1],
                    "confidence": r[2],
                    "text_cs": r[3],
                    "text_en": r[4],
                    "is_active": r[5],
                }
                for r in (await session.execute(text(_snapshot_sql()))).all()
            }

            # Second pass — must be a net no-op (idempotent UPSERT).
            await d.seed_disclaimers(session)
            second_rows = {
                r[0]: {
                    "answer_mode": r[1],
                    "confidence": r[2],
                    "text_cs": r[3],
                    "text_en": r[4],
                    "is_active": r[5],
                }
                for r in (await session.execute(text(_snapshot_sql()))).all()
            }
            return first_rows, second_rows, n_first
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_seed_registry_table_bijection_and_idempotent() -> None:
    if not await _db_reachable():
        pytest.skip("no DB / disclaimer table not reachable")

    first_rows, second_rows, n_first = await _seed_and_read_in_rolled_back_txn()

    registry_ids = set(d.list_disclaimer_ids())

    # Return count == registry size (one upsert per entry).
    assert n_first == len(registry_ids)

    # BIJECTION: the seeded id set equals the registry id set exactly —
    # every registry id seeds a row, the table holds no id the registry lacks.
    assert set(first_rows.keys()) == registry_ids

    # Exactly one row per id (dict keys are unique; count matches).
    assert len(first_rows) == len(registry_ids)

    # Each seeded row's content matches the in-code registry (no drift).
    for did, row in first_rows.items():
        block = d.get_disclaimer(did)
        assert row["answer_mode"] == block.answer_mode
        assert row["confidence"] == block.confidence
        assert row["text_cs"] == block.text_cs
        assert row["text_en"] == block.text_en
        assert row["is_active"] is True

    # IDEMPOTENT: a second seed leaves byte-identical rows (same ids, same content).
    assert first_rows == second_rows


@pytest.mark.asyncio
async def test_live_table_untouched_after_probe() -> None:
    """Belt-and-braces: the rollback-only probe left the live table as it found
    it (still empty at the live default)."""
    if not await _db_reachable():
        pytest.skip("no DB / disclaimer table not reachable")

    from sqlalchemy import text

    from regsuper.storage.database import get_session_factory

    factory = get_session_factory()
    async with factory() as session:
        n = (await session.execute(text("SELECT count(*) FROM disclaimer"))).scalar()
    # The probe rolled back; if the live table was empty before, it still is.
    assert int(n) == 0
