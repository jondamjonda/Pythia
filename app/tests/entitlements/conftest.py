"""Shared fixtures for the C2 entitlements (T-C2a) tests.

Two test strata:
  * DB-free (mocked AsyncSession) — the bulk of the resolver/guard/scope/depth
    logic, runnable in CI without Postgres.
  * DB-backed (Docker Postgres via the project's session_scope) — the floor
    resolve from the seeded price book, the 2nd-org/subscription fixture, and the
    TTL re-resolve. These SKIP cleanly if no DB is reachable so the offline
    subset stays green.

The seeded plan UUIDs are deterministic (009 fixed literals), so the fixtures can
reference them without a SELECT round-trip.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from regsuper.entitlements import cache

# 009 fixed-literal plan ids.
LITE_PLAN = UUID("00000000-0000-0000-0000-0000000009a1")
BUSINESS_PLAN = UUID("00000000-0000-0000-0000-0000000009a2")
ADVISOR_PLAN = UUID("00000000-0000-0000-0000-0000000009a3")

# The migration-007 sentinel org (a real organization row).
SENTINEL_ORG = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture(autouse=True)
def _clear_entitlement_cache():
    """Every test starts and ends with an empty resolver cache (isolation)."""
    cache.clear()
    yield
    cache.clear()


async def _db_reachable() -> bool:
    """True if the Docker Postgres named by DATABASE_URL answers a trivial query.

    Disposes the module-level engine first: it is a singleton bound to whichever
    event loop first created it, and pytest-asyncio runs each test in its OWN
    loop, so a pooled connection from a previous DB test would be on a dead loop.
    Disposing gives this test a fresh engine on the current loop — the same
    dispose-between-DB-tests pattern test_migration_chain / test_disclaimers_seed
    use.
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
    """A real session_scope-managed AsyncSession, or skip if no DB.

    Yields the session; the surrounding session_scope commits on clean exit. Each
    consuming test is responsible for cleaning up any rows it inserts (the suite
    must leave the seeded DB exactly as it found it).
    """
    if not await _db_reachable():
        pytest.skip("no Postgres reachable (DATABASE_URL) — DB-backed test skipped")
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        yield session


@pytest.fixture
async def second_org(db_session):
    """Insert a brand-new (2nd) org so resolver tests do not depend on / mutate the
    sentinel. Returns its UUID; the org + any of its subscriptions are removed on
    teardown so the seed is left pristine.

    Both INSERT and the teardown DELETE run on the SAME ``db_session`` (the live
    session_scope bound to this test's event loop), so the cleanup is reliable —
    a separate session_scope on teardown could land on a disposed/dead loop. The
    teardown happens before ``db_session`` tears down (reverse fixture order), so
    the delete is committed by db_session's session_scope exit.
    """
    from sqlalchemy import delete, text

    from regsuper.storage.models import Organization, Subscription

    org = Organization(name=f"test-org-{uuid4().hex[:8]}", org_type="sme")
    db_session.add(org)
    await db_session.flush()
    org_id = org.id
    try:
        yield org_id
    finally:
        await db_session.execute(delete(Subscription).where(Subscription.org_id == org_id))
        await db_session.execute(
            text("DELETE FROM organization WHERE id = :oid"), {"oid": str(org_id)}
        )
        await db_session.flush()
        cache.invalidate(org_id)
