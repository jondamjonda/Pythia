"""Entitlement resolution and pure projections over a resolved entitlement.

`effective_entitlement(session, org_id)` resolves an org's plan; `depth_allowed` and
`scopes_for_org` project the result onto an analysis-depth cap and a Conduit scope set. The
route guard, `require_entitlement`, lives in `guard.py`.

Invariants:

* Fail closed. Any resolver error (database unreachable, malformed row, missing floor plan)
  raises; it never falls back to a default or higher-tier grant. The only non-error fallback is
  the documented floor: an org with no current subscription resolves to the seeded `lite` plan.
* The `lite` floor is read from the database (the seeded plan and entitlement rows), not from
  a hardcoded default, so edits to the price book are honoured. If the `lite` row is missing
  the resolver raises: a corrupt price book is an error, never an implicit grant.
* Results are cached for 60 seconds (`cache.py`) and invalidated by
  `billing.on_subscription_changed(org_id)`.

The resolver reads public reference data (`plan`, `entitlement`; no row-level security) plus
the org's tenant-scoped `subscription`. Every query filters by org, so it stays correct under
row-level security.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from regsuper.entitlements import cache
from regsuper.entitlements.models import Depth, EffectiveEntitlement
from regsuper.storage.models import Entitlement as EntitlementRow
from regsuper.storage.models import Plan, Subscription

if TYPE_CHECKING:  # avoid importing the async session machinery at module load
    from sqlalchemy.ext.asyncio import AsyncSession

# The floor plan code: an org with no current subscription gets this plan's entitlement
# (read from the database).
_FLOOR_PLAN_CODE = "lite"

# Subscription statuses that count as a current, entitling subscription. A canceled
# subscription never entitles; 'past_due' still does (grace period). This matches the predicate
# of the ux_sub_one_active partial unique index.
_ACTIVE_STATUSES = ("trialing", "active", "past_due")


class EntitlementResolutionError(RuntimeError):
    """The entitlement cannot be resolved for a structural reason.

    Raised when the seeded floor plan or its entitlement row is missing, or a row is malformed.
    This is the fail-closed signal: the guard maps it to a 403, never a silent grant. It is
    distinct from the documented floor (no subscription resolves to `lite`), which is a normal
    resolution, not an error.
    """


def _row_to_entitlement(org_id: UUID, plan: Plan, ent: EntitlementRow) -> EffectiveEntitlement:
    """Project a (plan, entitlement) database row pair onto the in-memory value object."""
    return EffectiveEntitlement(
        org_id=org_id,
        plan_code=plan.code,
        tier=int(plan.tier),
        max_depth=ent.max_depth,  # 'L2'|'L3'|'L4'
        answer_mode=ent.answer_mode,
        queries_per_day=int(ent.queries_per_day),
        analyses_per_month=int(ent.analyses_per_month),
        max_seats=int(ent.max_seats),
        audit_export=bool(ent.audit_export),
        api_access=bool(ent.api_access),
        multi_client=bool(ent.multi_client),
        change_alerts=bool(ent.change_alerts),
        answer=bool(ent.answer),
        metadata=dict(ent.metadata_ or {}),
    )


async def _resolve_floor(session: AsyncSession, org_id: UUID) -> EffectiveEntitlement:
    """Resolve the seeded floor (`lite`) entitlement from the database.

    Raises EntitlementResolutionError if the floor plan or its entitlement row is absent: a
    corrupt price book is an error, never an implicit grant.
    """
    row = (
        await session.execute(
            select(Plan, EntitlementRow)
            .join(EntitlementRow, EntitlementRow.plan_id == Plan.id)
            .where(Plan.code == _FLOOR_PLAN_CODE)
        )
    ).first()
    if row is None:
        raise EntitlementResolutionError(
            f"floor plan {_FLOOR_PLAN_CODE!r} (or its entitlement row) is missing "
            "from the price book — cannot resolve a fail-closed floor (WP-044)."
        )
    plan, ent = row
    return _row_to_entitlement(org_id, plan, ent)


async def effective_entitlement(session: AsyncSession, org_id: UUID) -> EffectiveEntitlement:
    """Resolve the org's effective entitlement.

    Resolution order:

    1. A live cache hit is returned without a database round trip.
    2. Otherwise the org's current subscription (status trialing, active or past_due, and
       `valid_to IS NULL`) is joined to its plan and entitlement.
    3. With no current subscription, the seeded `lite` floor is read from the database.
    4. Any structural failure raises EntitlementResolutionError (fail closed, never a grant).

    A successful result, whether a subscription tier or the floor, is cached for 60 seconds and
    invalidated by `billing.on_subscription_changed(org_id)`.
    """
    cached = cache.get(org_id)
    if cached is not None:
        return cached

    # The org's current entitling subscription, joined to its plan and entitlement.
    # `valid_to IS NULL` marks the current row; the status filter excludes canceled ones. The
    # ux_sub_one_active index allows at most one such row, but order defensively by valid_from
    # DESC and take the first.
    row = (
        await session.execute(
            select(Plan, EntitlementRow)
            .join(Subscription, Subscription.plan_id == Plan.id)
            .join(EntitlementRow, EntitlementRow.plan_id == Plan.id)
            .where(
                Subscription.org_id == org_id,
                Subscription.valid_to.is_(None),
                Subscription.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(Subscription.valid_from.desc())
        )
    ).first()

    if row is not None:
        plan, ent = row
        result = _row_to_entitlement(org_id, plan, ent)
    else:
        # The documented floor, not a fail-open: an org with no current subscription is
        # entitled to the seeded `lite` tier.
        result = await _resolve_floor(session, org_id)

    cache.put(org_id, result)
    return result


# ---------------------------------------------------------------------------
# Pure projections over an already-resolved entitlement.
# ---------------------------------------------------------------------------
def _as_entitlement(org: Any) -> EffectiveEntitlement:
    """Accept either an `EffectiveEntitlement` or an object carrying its fields.

    The public signatures are `depth_allowed(org)` and `scopes_for_org(org)`; in practice the
    caller passes the resolved `EffectiveEntitlement`. This keeps the projections pure (no
    database access): resolution happens once, at the entry point.
    """
    if isinstance(org, EffectiveEntitlement):
        return org
    # Duck typing: anything exposing the entitlement fields is accepted (e.g. a test stub). A
    # bare org_id would need a database read, which is deliberately not done here.
    if hasattr(org, "max_depth") and hasattr(org, "api_access"):
        return org  # type: ignore[return-value]
    raise TypeError(
        "depth_allowed/scopes_for_org expect a resolved EffectiveEntitlement "
        "(call effective_entitlement first); got "
        f"{type(org).__name__}"
    )


def depth_allowed(org: Any) -> Depth:
    """Return the org's analysis-depth cap (`entitlement.max_depth`).

    Lite is capped at 'L2', Business and Advisor at 'L4'. This returns the cap only; the clamp
    lives at the entry point (`metering.clamp_layers`): a Lite org requesting L4 runs L2 and
    gets a `layers_capped` note. It is never a 403; the analysis still succeeds, downgraded
    with an explanation.
    """
    return _as_entitlement(org).max_depth


def scopes_for_org(org: Any) -> set[str]:
    """Project the org's entitlement onto its Conduit OAuth/MCP scope set.

    Returns an empty set when `entitlement.api_access` is False, so an org without API access
    (or one that was downgraded) cannot mint a token; otherwise the granted MCP scopes. Used by
    the Conduit module.
    """
    ent = _as_entitlement(org)
    if not ent.api_access:
        return set()
    # Orgs with api_access (Advisor, or per-org grants) get the read and answer scopes. The set
    # may be narrowed per org via entitlement.metadata['scopes']; otherwise the full default
    # set is granted.
    custom = ent.metadata.get("scopes") if isinstance(ent.metadata, dict) else None
    if isinstance(custom, (list, tuple, set)):
        return {str(s) for s in custom}
    return {"legislation.read", "obligations.read", "answer"}
