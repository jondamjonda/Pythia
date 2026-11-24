"""Provider-agnostic billing: an append-only event outbox plus pluggable payment providers.

Every billable signal (metered analyses, quota events, seat and subscription changes, Conduit
API calls) is written as one row to the append-only `billing_event` table before any provider
is called. The only provider shipped is the no-op default, so billing works without a payment
backend; Conduit calls share the same table through the nullable `conduit_call_id` column.
"""

from __future__ import annotations

from regsuper.billing.events import emit_billing_event, on_subscription_changed
from regsuper.billing.interface import (
    BILLING_EVENT_TYPES,
    BillingEvent,
    BillingProvider,
)
from regsuper.billing.noop import NoopBillingProvider

__all__ = [
    "BILLING_EVENT_TYPES",
    "BillingEvent",
    "BillingProvider",
    "NoopBillingProvider",
    "emit_billing_event",
    "on_subscription_changed",
]
