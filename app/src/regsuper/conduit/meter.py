"""Conduit metering: a quota precheck before each call and an api_call plus billing record after.

Each call goes through two phases:

* `precheck(org_id, tool)`, before any engine work: resolve the org's entitlement and reserve
  one unit of its daily `api_calls` quota in `usage_counter`. Over quota, `QuotaExceeded` (429,
  `quota.exceeded`, never 402) is raised before the engine is called, so no billable work is
  done, and a `quota.exceeded` billing event is emitted alongside it.
* `record(...)`, after the call, whether it succeeded, was refused or failed: write one
  `api_call` row with `provider="conduit"` (so it appears on the cost dashboard like any other
  provider) and one `billing_event` row with `event_type='conduit.call'`. A fresh
  `conduit_call_id` links the two rows. Refused calls are recorded at zero cost but still
  recorded, so abuse and funnel numbers stay visible.

Ownership: plans, quotas, `usage_counter`, `billing_event` and the entitlement functions belong
to the entitlements and billing packages. Conduit owns `conduit_credential`, this module,
`ratelimit.py`, its `provider="conduit"` api_call rows, and the emission of `conduit.call`
billing events.

Quota enforcement follows `entitlements_enabled` (there is no separate quota flag, since quotas
mean nothing without tier limits). With it off (the default), `precheck` resolves nothing,
reserves nothing and always allows the call; `record` still writes both rows, so Conduit usage
is observable from the start.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

_WP = "WP-062 (Stage-2 Conduit build)"

# The usage_counter metric each Conduit tool call reserves against: the org's daily call
# allowance. 'api_calls' is a daily bucket in observability.limits._METRIC_PERIOD_KIND, so the
# cap resets at UTC midnight.
_CONDUIT_METRIC = "api_calls"


@dataclass(slots=True)
class MeterResult:
    """Outcome of `precheck`: whether the call may proceed, and the id that links its records.

    `conduit_call_id` (a uuid4 string) is minted in `precheck` and passed on to `record`, so
    the api_call and billing_event rows share one id. When `allowed` is False the caller
    raises the 429 quota error and does no engine work.
    """

    allowed: bool
    conduit_call_id: str
    reason: str | None = None  # set when allowed is False (e.g. 'quota.exceeded')


async def precheck(org_id: str, tool: str) -> MeterResult:
    """Resolve the entitlement and reserve quota before `tool` runs.

    With `entitlements_enabled` off (the default) this resolves nothing, reserves nothing and
    returns `allowed=True` with a fresh `conduit_call_id`.

    With it on, it resolves `effective_entitlement(org)` and reserves one unit of the org's
    daily `api_calls` counter via `check_and_reserve_quota`. Over quota, that raises
    `QuotaExceeded` (429, 'quota.exceeded') before any engine call, so nothing billable is
    consumed, and emits the matching `quota.exceeded` billing event. `precheck` does not
    swallow the exception; the REST or MCP adapter maps it to a 429 (see errors.py). `org_id`
    must be the principal's real org (the column is NOT NULL).
    """
    conduit_call_id = str(uuid4())

    from regsuper.config import get_settings

    if not get_settings().entitlements_enabled:
        # Quotas are not enforced while entitlements are off.
        return MeterResult(allowed=True, conduit_call_id=conduit_call_id)

    from regsuper.entitlements.resolver import effective_entitlement
    from regsuper.observability.limits import check_and_reserve_quota
    from regsuper.storage.database import session_scope

    org_uuid = UUID(str(org_id))
    async with session_scope() as session:
        ent = await effective_entitlement(session, org_uuid)
        # The org's daily call allowance. -1 means unlimited: check_and_reserve_quota still
        # increments the counter for reporting but never raises 429.
        limit = int(getattr(ent, "queries_per_day", -1))
        # Over the limit this refunds the increment, emits the quota.exceeded billing event,
        # and raises QuotaExceeded (429), which propagates to the caller.
        await check_and_reserve_quota(session, org_uuid, _CONDUIT_METRIC, limit=limit)

    return MeterResult(allowed=True, conduit_call_id=conduit_call_id)


async def record(
    org_id: str,
    tool: str,
    conduit_call_id: str,
    *,
    refused: bool = False,
    amount_usd: float = 0.0,
) -> None:
    """Write one api_call row and one `conduit.call` billing event for a finished call.

    Called on success, refusal and error alike. Writes exactly one `api_call` row
    (`provider="conduit"`, `source="conduit.<tool>"`, `org_id` set) and calls
    `emit_billing_event(event_type='conduit.call', ...)` with the same `conduit_call_id`.
    Refused calls are billed at zero but still recorded, so abuse and funnel numbers stay
    visible. Both writes share one transaction, so the linked pair is atomic. `org_id` must be
    the principal's real org (NOT NULL).

    For data minimisation the query text is never stored; only metadata (tool, refused flag,
    call id) goes into `request_json` and the billing payload.
    """
    from regsuper.billing.events import emit_billing_event
    from regsuper.storage.database import session_scope
    from regsuper.storage.models import ApiCall

    org_uuid = UUID(str(org_id))
    call_uuid = UUID(str(conduit_call_id))
    status = "refused" if refused else "ok"
    bill_amount = Decimal("0") if refused else Decimal(str(amount_usd))

    async with session_scope() as session:
        # 1) One api_call row, the system of record (same shape as api_tracker._persist_row).
        session.add(
            ApiCall(
                provider="conduit",
                api_key_id=f"conduit:org:{org_id}",
                model="n/a",
                endpoint=tool,
                source=f"conduit.{tool}",
                project_id=None,  # Conduit is not project-scoped
                org_id=org_uuid,
                tokens_input=0,
                tokens_output=0,
                tokens_cached=0,
                cost_usd=float(amount_usd),
                latency_ms=0,
                status=status,
                # Metadata only; never the query text.
                request_json={
                    "tool": tool,
                    "refused": refused,
                    "conduit_call_id": str(call_uuid),
                },
                response_summary=f"conduit.{tool} refused={refused}",
            )
        )
        await session.flush()

        # 2) One conduit.call billing event with the same conduit_call_id, written through the
        #    billing package's emitter (Conduit does not own the billing table).
        await emit_billing_event(
            session,
            org_uuid,
            "conduit.call",
            conduit_call_id=call_uuid,
            amount_usd=bill_amount,
            currency="USD",
            payload={
                "tool": tool,
                "billable_unit": "refused" if refused else tool,
                "refused": refused,
                "conduit_call_id": str(call_uuid),
            },
        )


__all__ = ["MeterResult", "precheck", "record"]
