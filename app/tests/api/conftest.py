"""Shared fixtures for the C2c metering / tier-clamp API tests.

Mirrors tests/observability/conftest.py + tests/entitlements/conftest.py:
  * `db_session` disposes the singleton engine first (per-test event loop) then
    yields a real session_scope-managed AsyncSession; SKIPs cleanly without a
    reachable Docker Postgres.
  * `metering_org` inserts a fresh org + tears down every row referencing it
    (analysis_run / usage_counter / billing_event / subscription / api_call /
    finding / project), so the seeded DB is left pristine. billing_event is
    append-only — the teardown disables its no-modify trigger for the DELETE (the
    test connection runs as the superuser owner).
  * `subscribe` helper inserts a CURRENT subscription so the resolver returns a
    non-floor tier; the entitlement cache is cleared around every test.

These exercise project_analyze's metering write-through with the cascade runners
patched, so no live LLM is ever called.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from regsuper.entitlements import cache

# 009 fixed-literal plan ids.
LITE_PLAN = "00000000-0000-0000-0000-0000000009a1"
BUSINESS_PLAN = "00000000-0000-0000-0000-0000000009a2"
ADVISOR_PLAN = "00000000-0000-0000-0000-0000000009a3"


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
    if not await _db_reachable():
        pytest.skip("no Postgres reachable (DATABASE_URL) — DB-backed test skipped")
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        yield session


@pytest.fixture
async def metering_org(db_session):
    """Insert a fresh org; tear down EVERY row referencing it on the same session."""
    from sqlalchemy import delete, text

    from regsuper.storage.models import (
        AnalysisRun,
        ApiCall,
        Finding,
        Organization,
        Project,
        Subscription,
        UsageCounter,
    )

    org = Organization(name=f"c2c-org-{uuid4().hex[:8]}", org_type="sme")
    db_session.add(org)
    await db_session.flush()
    org_id = org.id
    try:
        yield org_id
    finally:
        # FK-respecting teardown order: dependent rows first.
        await db_session.execute(delete(ApiCall).where(ApiCall.org_id == org_id))
        await db_session.execute(delete(Finding).where(Finding.org_id == org_id))
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
        await db_session.execute(delete(Project).where(Project.org_id == org_id))
        await db_session.execute(delete(Subscription).where(Subscription.org_id == org_id))
        await db_session.execute(
            text("DELETE FROM organization WHERE id = :oid"), {"oid": str(org_id)}
        )
        await db_session.flush()
        cache.invalidate(org_id)


async def subscribe(session, org_id, plan_id: str) -> None:
    """Insert a CURRENT (valid_to NULL, status active) subscription for ``org_id``
    so effective_entitlement returns that plan's tier instead of the lite floor."""
    from uuid import UUID

    from regsuper.storage.models import Subscription

    session.add(Subscription(org_id=org_id, plan_id=UUID(plan_id), status="active"))
    await session.flush()
    cache.invalidate(org_id)
