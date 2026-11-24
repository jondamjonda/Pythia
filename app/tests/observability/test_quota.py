"""QUO-1/2 — check_and_reserve_quota (atomic reserve, -1 unlimited, 429-before-
spend, coupled quota.exceeded billing_event), record_usage, refund_quota,
org_usage_summary (WP-045 / T-C2b).

DB-backed against the seeded Docker Postgres; skips cleanly without it. The
concurrency test runs N genuinely-parallel reserves on SEPARATE connections
(session_scope per task) to exercise the atomic UPSERT, so it commits its own org
first (a sibling connection cannot see an uncommitted row).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from regsuper.observability.limits import (
    QuotaExceeded,
    check_and_reserve_quota,
    org_usage_summary,
    period_start,
    record_usage,
    refund_quota,
)
from regsuper.storage.models import BillingEvent as BillingEventRow
from regsuper.storage.models import UsageCounter


async def _count(session, org_id, metric, now=None) -> int:
    ps = period_start(metric, now)
    row = (
        await session.execute(
            select(UsageCounter.count).where(
                UsageCounter.org_id == org_id,
                UsageCounter.metric == metric,
                UsageCounter.period_start == ps,
            )
        )
    ).scalar_one_or_none()
    return int(row or 0)


# ---------------------------------------------------------------------------
# period_start bucketing (pure, DB-free).
# ---------------------------------------------------------------------------
def test_period_start_day_vs_month():
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    assert period_start("queries", now).isoformat() == "2026-06-15"
    assert period_start("api_calls", now).isoformat() == "2026-06-15"
    assert period_start("analyses", now).isoformat() == "2026-06-01"
    assert period_start("analyses_L4", now).isoformat() == "2026-06-01"
    # Unknown metric → conservative month bucket.
    assert period_start("mystery", now).isoformat() == "2026-06-01"


def test_quota_exceeded_carries_status_and_code():
    exc = QuotaExceeded("analyses", 3, count=4)
    assert exc.status_code == 429
    assert exc.code == "quota.exceeded"
    assert exc.metric == "analyses"
    assert exc.limit == 3


# ---------------------------------------------------------------------------
# Reserve up to the limit; the (limit+1)th raises 429 BEFORE spend.
# ---------------------------------------------------------------------------
async def test_fourth_reserve_at_limit_three_raises_429(db_session, metering_org):
    """Lite analyses_per_month=3 → reserves 1..3 succeed, the 4th = 429 before any
    cascade, and the final count is 3 (the over-quota reserve refunded)."""
    now = datetime.now(UTC)
    for i in range(1, 4):
        n = await check_and_reserve_quota(db_session, metering_org, "analyses", limit=3, now=now)
        assert n == i

    with pytest.raises(QuotaExceeded) as ei:
        await check_and_reserve_quota(db_session, metering_org, "analyses", limit=3, now=now)
    assert ei.value.status_code == 429
    assert ei.value.code == "quota.exceeded"

    # Refunded: the counter still reads exactly the limit, not limit+1.
    assert await _count(db_session, metering_org, "analyses", now) == 3

    # Coupled quota.exceeded billing_event written exactly once.
    n_be = (
        await db_session.execute(
            select(func.count(BillingEventRow.id)).where(
                BillingEventRow.org_id == metering_org,
                BillingEventRow.event_type == "quota.exceeded",
            )
        )
    ).scalar_one()
    assert n_be == 1


async def test_unlimited_limit_never_raises(db_session, metering_org):
    """limit == -1 (advisor) → reserve increments for usage reporting, never 429."""
    now = datetime.now(UTC)
    for i in range(1, 11):
        n = await check_and_reserve_quota(db_session, metering_org, "analyses", limit=-1, now=now)
        assert n == i
    assert await _count(db_session, metering_org, "analyses", now) == 10
    # No quota.exceeded billing_event was ever emitted for unlimited.
    n_be = (
        await db_session.execute(
            select(func.count(BillingEventRow.id)).where(
                BillingEventRow.org_id == metering_org,
                BillingEventRow.event_type == "quota.exceeded",
            )
        )
    ).scalar_one()
    assert n_be == 0


async def test_zero_quota_org_first_reserve_raises(db_session, metering_org):
    """A 0-limit org → the very first reserve raises 429 and the counter is 0."""
    now = datetime.now(UTC)
    with pytest.raises(QuotaExceeded):
        await check_and_reserve_quota(db_session, metering_org, "analyses", limit=0, now=now)
    assert await _count(db_session, metering_org, "analyses", now) == 0


# ---------------------------------------------------------------------------
# refund_quota / record_usage / org_usage_summary.
# ---------------------------------------------------------------------------
async def test_refund_decrements_a_reservation(db_session, metering_org):
    """A refused run refunds its reserved unit (does not consume quota)."""
    now = datetime.now(UTC)
    await check_and_reserve_quota(db_session, metering_org, "analyses", limit=3, now=now)
    assert await _count(db_session, metering_org, "analyses", now) == 1
    await refund_quota(db_session, metering_org, "analyses", now=now)
    assert await _count(db_session, metering_org, "analyses", now) == 0
    # Floors at 0 — a double refund never goes negative.
    await refund_quota(db_session, metering_org, "analyses", now=now)
    assert await _count(db_session, metering_org, "analyses", now) == 0


async def test_record_usage_rolls_cost_without_touching_count(db_session, metering_org):
    now = datetime.now(UTC)
    await check_and_reserve_quota(db_session, metering_org, "analyses", limit=3, now=now)
    await record_usage(db_session, metering_org, "analyses", cost_usd=0.25, now=now)
    await record_usage(db_session, metering_org, "analyses", cost_usd=0.10, now=now)
    # Count unchanged by record_usage; cost accumulated.
    assert await _count(db_session, metering_org, "analyses", now) == 1
    ps = period_start("analyses", now)
    cost = (
        await db_session.execute(
            select(UsageCounter.cost_usd).where(
                UsageCounter.org_id == metering_org,
                UsageCounter.metric == "analyses",
                UsageCounter.period_start == ps,
            )
        )
    ).scalar_one()
    assert float(cost) == pytest.approx(0.35)


async def test_record_usage_zero_cost_is_noop(db_session, metering_org):
    now = datetime.now(UTC)
    await record_usage(db_session, metering_org, "analyses", cost_usd=0.0, now=now)
    # No row created for a zero-cost no-op (nothing reserved, nothing to roll).
    assert await _count(db_session, metering_org, "analyses", now) == 0


async def test_org_usage_summary_reports_live_counts(db_session, metering_org):
    now = datetime.now(UTC)
    await check_and_reserve_quota(db_session, metering_org, "analyses", limit=3, now=now)
    await check_and_reserve_quota(db_session, metering_org, "queries", limit=10, now=now)
    await record_usage(db_session, metering_org, "analyses", cost_usd=0.5, now=now)

    summary = await org_usage_summary(db_session, metering_org, now=now)
    assert summary["analyses"]["count"] == 1
    assert summary["analyses"]["cost_usd"] == pytest.approx(0.5)
    assert summary["analyses"]["period_kind"] == "month"
    assert summary["queries"]["count"] == 1
    assert summary["queries"]["period_kind"] == "day"


# ---------------------------------------------------------------------------
# Concurrency — N parallel reserves at limit K admit exactly K (atomic UPSERT).
# ---------------------------------------------------------------------------
async def test_parallel_reserves_admit_exactly_the_limit():
    """10 genuinely-parallel reserves (separate connections) at limit 3 → exactly
    3 succeed, 7 raise QuotaExceeded, and the final count is 3."""
    from regsuper.storage.database import dispose_engine, session_scope
    from regsuper.storage.models import Organization

    # Skip cleanly without a DB.
    await dispose_engine()
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("no Postgres reachable (DATABASE_URL) — DB-backed test skipped")

    # Commit a fresh org FIRST so the sibling connections can see it (FK).
    org_id = uuid4()
    async with session_scope() as s:
        s.add(Organization(id=org_id, name=f"conc-org-{org_id.hex[:8]}", org_type="sme"))

    now = datetime.now(UTC)
    limit = 3

    async def _one():
        # Each reserve on its OWN connection/transaction — genuine concurrency.
        async with session_scope() as s:
            try:
                await check_and_reserve_quota(s, org_id, "analyses", limit=limit, now=now)
                return "ok"
            except QuotaExceeded:
                return "429"

    try:
        results = await asyncio.gather(*[_one() for _ in range(10)])
        assert results.count("ok") == limit
        assert results.count("429") == 10 - limit
        # Final count is exactly the limit (refunds applied).
        async with session_scope() as s:
            ps = period_start("analyses", now)
            count = (
                await s.execute(
                    select(UsageCounter.count).where(
                        UsageCounter.org_id == org_id,
                        UsageCounter.metric == "analyses",
                        UsageCounter.period_start == ps,
                    )
                )
            ).scalar_one()
            assert int(count) == limit
    finally:
        # Cleanup: drop usage_counter + billing_event (append-only trigger off) + org.
        async with session_scope() as s:
            await s.execute(text("DELETE FROM usage_counter WHERE org_id = :o"), {"o": str(org_id)})
            await s.execute(
                text("ALTER TABLE billing_event DISABLE TRIGGER trg_billing_event_no_update")
            )
            await s.execute(text("DELETE FROM billing_event WHERE org_id = :o"), {"o": str(org_id)})
            await s.execute(
                text("ALTER TABLE billing_event ENABLE TRIGGER trg_billing_event_no_update")
            )
            await s.execute(text("DELETE FROM organization WHERE id = :o"), {"o": str(org_id)})
