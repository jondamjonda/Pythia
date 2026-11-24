"""Billing provider contract and the billing event value object.

`BillingProvider` is the protocol every payment backend implements; `BillingEvent` mirrors a
row of the append-only `billing_event` table. `BILLING_EVENT_TYPES` exposes the table's CHECK
set so call sites can validate before inserting. The only provider shipped is the no-op in
`noop.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

# The exact event_type CHECK set on billing_event (migration 011). `conduit.call` rows are
# written by the Conduit module through this package's `emit_billing_event`.
BILLING_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "analysis.metered",
        "quota.exceeded",
        "seat.added",
        "seat.removed",
        "subscription.changed",
        "conduit.call",
    }
)


@dataclass(frozen=True, slots=True)
class BillingEvent:
    """One billing signal, shaped like a row of the `billing_event` table.

    Mirrors the durable columns: `event_type` from the CHECK set, an optional
    `analysis_run_id` (for `analysis.metered`) and `conduit_call_id` (Conduit attribution, no
    foreign key), `amount_usd` (NUMERIC(12,6)) with `currency` (EUR for advisor seats, CZK for
    subscriptions), and a free-form `payload`. A metered event should also carry token counts
    in `payload` so a model with no known price still produces a billable signal.
    """

    org_id: UUID
    event_type: str
    analysis_run_id: int | None = None
    conduit_call_id: UUID | None = None
    amount_usd: Decimal | None = None
    currency: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class BillingProvider(Protocol):
    """A pluggable payment or metering backend (for example Stripe, Paddle, or the no-op).

    `handle()` is fire-and-forget: `emit_billing_event` calls it after the durable
    `billing_event` row is written and swallows any exception it raises, so a provider outage
    never fails a request. The default `NoopBillingProvider` satisfies this protocol and never
    raises.
    """

    async def handle(self, event: BillingEvent) -> None:
        """Forward one already-persisted billing event to the provider."""
        ...
