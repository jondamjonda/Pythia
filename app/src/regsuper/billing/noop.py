"""No-op billing provider, the default and only provider shipped (`billing_provider='noop'`).

Its `handle()` deliberately accepts and drops every event: there is no payment backend to
forward to, and a billing provider must never raise into the caller.
"""

from __future__ import annotations

from regsuper.billing.interface import BillingEvent, BillingProvider


class NoopBillingProvider:
    """Satisfies `BillingProvider`; accepts every event and does nothing.

    The durable `billing_event` row is written by `emit_billing_event` before the provider is
    called, so dropping the event here loses no billing signal. Accepting everything is the
    intended behaviour, not a placeholder.
    """

    async def handle(self, _event: BillingEvent) -> None:
        # Intentional no-op: the durable billing_event row is already written and there is no
        # provider to forward to.
        return None


# Module-level default instance used by billing.events when billing_provider='noop'.
_default_provider: BillingProvider = NoopBillingProvider()
