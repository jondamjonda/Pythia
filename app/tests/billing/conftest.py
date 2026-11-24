"""Shared fixtures for the C2 billing (T-C2a + T-C2b) tests.

The cache-clear fixture serves the T-C2a cache-invalidation tests. The DB-backed
`db_session` / `metering_org` fixtures serve the T-C2b `emit_billing_event` tests
(append-only insert, provider-swallow, the append-only trigger probe). They mirror
tests/entitlements/conftest.py: dispose the singleton engine before opening a
session (per-test event loop), INSERT + cleanup on the SAME yielded session, and
SKIP cleanly without Postgres.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from regsuper.entitlements import cache


@pytest.fixture(autouse=True)
def _clear_entitlement_cache():
    cache.clear()
    yield
    cache.clear()


async def _db_reachable() -> bool:
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
    """Insert a fresh org; tear down every billing/metering row referencing it.

    Teardown deletes usage_counter, billing_event (append-only trigger disabled
    for the statement; the test connection is the superuser owner), analysis_run,
    subscription, then the org — all on the SAME db_session.
    """
    from sqlalchemy import delete, text

    from regsuper.storage.models import (
        AnalysisRun,
        Organization,
        Subscription,
        UsageCounter,
    )

    org = Organization(name=f"bill-org-{uuid4().hex[:8]}", org_type="sme")
    db_session.add(org)
    await db_session.flush()
    org_id = org.id
    try:
        yield org_id
    finally:
        await db_session.execute(delete(UsageCounter).where(UsageCounter.org_id == org_id))
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
