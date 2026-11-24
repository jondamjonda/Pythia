"""BIL-1 — emit_billing_event: one append-only row, provider fire-and-forget,
invalid type raises, append-only trigger rejects UPDATE/DELETE (WP-046 / T-C2b).

DB-backed rows run against the seeded Docker Postgres and skip cleanly without it.
The provider-swallow path is exercised both DB-backed (a raising provider does not
500 the durable write) and structurally (the no-op provider).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from regsuper.billing import events
from regsuper.billing.interface import BILLING_EVENT_TYPES, BillingEvent
from regsuper.storage.models import BillingEvent as BillingEventRow


async def _count(session, org_id) -> int:
    return (
        await session.execute(
            select(func.count(BillingEventRow.id)).where(BillingEventRow.org_id == org_id)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Validation — invalid event_type raises BEFORE any insert.
# ---------------------------------------------------------------------------
async def test_invalid_event_type_raises_no_insert(db_session, metering_org):
    before = await _count(db_session, metering_org)
    with pytest.raises(ValueError):
        await events.emit_billing_event(db_session, metering_org, "not.a.real.type")
    # No row was written.
    assert await _count(db_session, metering_org) == before


@pytest.mark.parametrize("event_type", sorted(BILLING_EVENT_TYPES))
async def test_all_check_set_types_are_accepted(db_session, metering_org, event_type):
    """Every event_type in the 011 CHECK set inserts exactly one row."""
    before = await _count(db_session, metering_org)
    ev = await events.emit_billing_event(db_session, metering_org, event_type)
    assert isinstance(ev, BillingEvent)
    assert ev.event_type == event_type
    assert ev.org_id == metering_org
    assert await _count(db_session, metering_org) == before + 1


# ---------------------------------------------------------------------------
# Exactly one row per metered event; org_id NOT-NULL; payload carried.
# ---------------------------------------------------------------------------
async def test_one_metered_event_writes_exactly_one_row(db_session, metering_org):
    before = await _count(db_session, metering_org)
    ev = await events.emit_billing_event(
        db_session,
        metering_org,
        "analysis.metered",
        amount_usd=Decimal("0.123456"),
        currency="CZK",
        payload={"tokens_input": 100, "tokens_output": 50},
    )
    assert await _count(db_session, metering_org) == before + 1
    # The row carries the real org (NOT NULL), the amount, and the payload signal.
    row = (
        (
            await db_session.execute(
                select(BillingEventRow)
                .where(BillingEventRow.org_id == metering_org)
                .order_by(BillingEventRow.id.desc())
            )
        )
        .scalars()
        .first()
    )
    assert row.org_id == metering_org
    assert row.event_type == "analysis.metered"
    assert row.payload["tokens_input"] == 100
    assert row.payload["tokens_output"] == 50
    assert ev.amount_usd == Decimal("0.123456")


async def test_quota_exceeded_event_writes_one_row(db_session, metering_org):
    before = await _count(db_session, metering_org)
    await events.emit_billing_event(
        db_session,
        metering_org,
        "quota.exceeded",
        payload={"metric": "analyses", "limit": 3, "count": 4},
    )
    assert await _count(db_session, metering_org) == before + 1


# ---------------------------------------------------------------------------
# Provider fire-and-forget — a raising provider is swallowed (no 500), the
# durable row still lands.
# ---------------------------------------------------------------------------
async def test_provider_exception_is_swallowed(db_session, metering_org, monkeypatch):
    class _BoomProvider:
        async def handle(self, _event) -> None:
            raise RuntimeError("payment backend outage")

    monkeypatch.setattr(events, "_provider", lambda: _BoomProvider())

    before = await _count(db_session, metering_org)
    # Does NOT raise — the provider error is swallowed by contract.
    ev = await events.emit_billing_event(db_session, metering_org, "analysis.metered")
    assert ev.event_type == "analysis.metered"
    # The durable row was written BEFORE the provider call, so it persists.
    assert await _count(db_session, metering_org) == before + 1


async def test_provider_is_called_with_the_event(db_session, metering_org, monkeypatch):
    seen: list = []

    class _RecordingProvider:
        async def handle(self, event) -> None:
            seen.append(event)

    monkeypatch.setattr(events, "_provider", lambda: _RecordingProvider())
    ev = await events.emit_billing_event(db_session, metering_org, "seat.added")
    assert len(seen) == 1
    assert seen[0] is ev


# ---------------------------------------------------------------------------
# Append-only — the DB trigger rejects UPDATE and DELETE.
# ---------------------------------------------------------------------------
async def test_billing_event_rejects_update(db_session, metering_org):
    await events.emit_billing_event(db_session, metering_org, "analysis.metered")
    await db_session.flush()
    row = (
        (
            await db_session.execute(
                select(BillingEventRow)
                .where(BillingEventRow.org_id == metering_org)
                .order_by(BillingEventRow.id.desc())
            )
        )
        .scalars()
        .first()
    )
    # SAVEPOINT so only the failing statement rolls back — the outer transaction
    # (and the metering_org row) stays intact for the fixture teardown.
    with pytest.raises(Exception):  # noqa: B017 — DB raises 'billing_event is append-only'
        async with db_session.begin_nested():
            await db_session.execute(
                text("UPDATE billing_event SET currency = 'EUR' WHERE id = :id"),
                {"id": row.id},
            )
    # The row is unchanged (the UPDATE was rejected).
    after = (
        await db_session.execute(
            select(BillingEventRow.currency).where(BillingEventRow.id == row.id)
        )
    ).scalar_one()
    assert after != "EUR"


async def test_billing_event_rejects_delete(db_session, metering_org):
    await events.emit_billing_event(db_session, metering_org, "analysis.metered")
    await db_session.flush()
    row = (
        (
            await db_session.execute(
                select(BillingEventRow)
                .where(BillingEventRow.org_id == metering_org)
                .order_by(BillingEventRow.id.desc())
            )
        )
        .scalars()
        .first()
    )
    with pytest.raises(Exception):  # noqa: B017
        async with db_session.begin_nested():
            await db_session.execute(
                text("DELETE FROM billing_event WHERE id = :id"), {"id": row.id}
            )
    # The row still exists (the DELETE was rejected).
    still = (
        await db_session.execute(
            select(func.count(BillingEventRow.id)).where(BillingEventRow.id == row.id)
        )
    ).scalar_one()
    assert still == 1
