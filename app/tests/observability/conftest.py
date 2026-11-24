"""Shared fixtures for the C2 quota (T-C2b) DB-backed tests.

Mirrors tests/entitlements/conftest.py exactly:
  * `db_session` disposes the module-level engine first (it is a singleton bound
    to the loop that created it; pytest-asyncio gives each test its own loop), so
    a fresh engine is created on the current loop. INSERT + cleanup run on the
    SAME yielded session (a separate session_scope on teardown lands on a dead
    loop and orphans rows). SKIPs cleanly if no Postgres is reachable.
  * `metering_org` inserts a fresh org and tears down EVERY row that references it
    (usage_counter / billing_event / analysis_run / subscription) on the same
    session, so the seeded DB is left pristine. billing_event is append-only (a
    DB trigger blocks DELETE) — the teardown disables that trigger for the
    statement (the test connection runs as the superuser owner) so a billing row
    written by check_and_reserve_quota's coupled quota.exceeded event can be
    cleaned up.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from regsuper.entitlements import cache

# The migration-007 sentinel org (a real organization row).
SENTINEL_ORG = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _clear_entitlement_cache():
    cache.clear()
    yield
    cache.clear()


async def _db_reachable() -> bool:
    """True if the Docker Postgres named by DATABASE_URL answers a trivial query.

    Disposes the module-level engine first (singleton bound to the first event
    loop; pytest-asyncio runs each test in its OWN loop) so this test gets a fresh
    engine on the current loop.
    """
    from sqlalchemy import text

    from regsuper.storage.database import dispose_engine, session_scope

    await dispose_engine()
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def db_session():
    """A real session_scope-managed AsyncSession, or skip if no DB."""
    if not await _db_reachable():
        pytest.skip("no Postgres reachable (DATABASE_URL) — DB-backed test skipped")
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        yield session


@pytest.fixture
async def metering_org(db_session):
    """Insert a fresh org; tear down every metering row referencing it.

    Returns the org's UUID. Teardown (reverse fixture order, committed by the
    db_session session_scope exit) deletes usage_counter, billing_event (with the
    append-only trigger disabled for the statement), analysis_run, subscription,
    then the organization row — all on the SAME db_session.
    """
    from sqlalchemy import delete, text

    from regsuper.storage.models import (
        AnalysisRun,
        Organization,
        Subscription,
        UsageCounter,
    )

    org = Organization(name=f"meter-org-{uuid4().hex[:8]}", org_type="sme")
    db_session.add(org)
    await db_session.flush()
    org_id = org.id
    try:
        yield org_id
    finally:
        await db_session.execute(delete(UsageCounter).where(UsageCounter.org_id == org_id))
        # billing_event is append-only — disable its no-modify trigger for this
        # cleanup DELETE (the test connection is the superuser owner).
        await db_session.execute(
            text("ALTER TABLE billing_event DISABLE TRIGGER trg_billing_event_no_update")
        )
        await db_session.execute(
            text("DELETE FROM billing_event WHERE org_id = :oid"), {"oid": str(org_id)}
        )
        await db_session.execute(
            text("ALTER TABLE billing_event ENABLE TRIGGER trg_billing_event_no_update")
        )
        await db_session.execute(delete(AnalysisRun).where(AnalysisRun.org_id == org_id))
        await db_session.execute(delete(Subscription).where(Subscription.org_id == org_id))
        await db_session.execute(
            text("DELETE FROM organization WHERE id = :oid"), {"oid": str(org_id)}
        )
        await db_session.flush()
        cache.invalidate(org_id)
