"""Billing outbox writer and entitlement-cache invalidation hook.

`emit_billing_event` appends exactly one row to the append-only `billing_event` table and then
notifies the configured provider. Both the product billing path and the Conduit API write to
this one table.

Invariants:

* `org_id` is never null (the column is NOT NULL). Callers pass the real `Principal.org_id`.
* The durable row is written before the provider is called, so a provider outage loses no
  billing signal, and a provider exception is swallowed rather than turned into a 500.
* An invalid `event_type` (outside the table's CHECK set) raises before anything is inserted.
* Append-only: corrections are new rows; a database trigger rejects UPDATE and DELETE.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

from regsuper.billing import noop
from regsuper.billing.interface import (
    BILLING_EVENT_TYPES,
    BillingEvent,
    BillingProvider,
)
from regsuper.config import get_settings
from regsuper.entitlements import cache
from regsuper.storage.models import BillingEvent as BillingEventRow

if TYPE_CHECKING:  # keep the async-session import out of module load
    from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger(__name__)


def _provider() -> BillingProvider:
    """Resolve the configured billing provider.

    `noop` is the only provider (`config.billing_provider` is `Literal['noop']`). An unknown
    value falls back to the no-op instead of raising.
    """
    name = get_settings().billing_provider
    if name == "noop":
        return noop._default_provider
    # Defensive: an unrecognised provider name degrades to the safe no-op rather
    # than raising into the metered path. The durable row is already written.
    _log.warning("billing.provider.unknown name=%s -> noop", name)
    return noop._default_provider


async def emit_billing_event(
    session: AsyncSession,
    org_id: UUID,
    event_type: str,
    *,
    analysis_run_id: int | None = None,
    conduit_call_id: UUID | None = None,
    amount_usd: Decimal | None = None,
    currency: str | None = None,
    payload: dict[str, Any] | None = None,
) -> BillingEvent:
    """Append exactly one `billing_event` row, then notify the provider.

    Validates `event_type` against the table's CHECK set (`BILLING_EVENT_TYPES`), inserts one
    append-only row (the `trg_billing_event_no_update` trigger blocks UPDATE and DELETE), then
    calls `provider.handle()`, swallowing any provider exception so a payment outage cannot
    fail the caller. Token counts and other billable details go in `payload`, so a model with
    no known price still produces a row.

    The row is written within the passed session; committing the transaction is the caller's
    responsibility.

    Returns:
        The constructed `BillingEvent`.

    Raises:
        ValueError: `event_type` is not a valid billing event type (no row is written).
    """
    if event_type not in BILLING_EVENT_TYPES:
        # Raise before any insert so an invalid type leaves no partial billing signal.
        raise ValueError(
            f"invalid billing event_type {event_type!r}; "
            f"must be one of {sorted(BILLING_EVENT_TYPES)}"
        )

    event = BillingEvent(
        org_id=org_id,
        event_type=event_type,
        analysis_run_id=analysis_run_id,
        conduit_call_id=conduit_call_id,
        amount_usd=amount_usd,
        currency=currency,
        payload=dict(payload or {}),
    )

    # 1) Write the durable append-only row first (org_id is NOT NULL: always the real org).
    row = BillingEventRow(
        org_id=event.org_id,
        event_type=event.event_type,
        analysis_run_id=event.analysis_run_id,
        conduit_call_id=event.conduit_call_id,
        amount_usd=event.amount_usd,
        currency=event.currency,
        payload=event.payload,
    )
    session.add(row)
    await session.flush()  # surface a CHECK/NOT-NULL violation here, not later

    # 2) Notify the provider fire-and-forget; a provider outage must never fail the request.
    try:
        await _provider().handle(event)
    except Exception:
        _log.warning(
            "billing.provider.error event_type=%s org_id=%s (swallowed)",
            event.event_type,
            event.org_id,
            exc_info=True,
        )

    return event


async def on_subscription_changed(session: AsyncSession, org_id: UUID) -> None:
    """Invalidate the org's entitlement cache after a subscription change.

    Clears the org's entry in the 60-second resolver cache so the next
    `effective_entitlement(org_id)` call resolves the new plan immediately. This is purely
    in-process and writes no row.

    `session` is unused today; it keeps the signature consistent with the rest of the billing
    API so that a later version can also record a `subscription.changed` billing event.
    """
    cache.invalidate(org_id)
