"""FastAPI route guard for entitlement features, plus the standard denial status codes.

`require_entitlement(feature, min_tier)` returns an async dependency that behaves as follows:

* With `entitlements_enabled` off (the default) it returns None and the route runs unguarded.
* With it on, it resolves the caller's entitlement and checks the boolean `feature` column
  (and the optional `min_tier`):

  - feature not included in the tier: 402 (`HTTP_PAYMENT_REQUIRED`, upgrade path);
  - org not entitled, or below `min_tier`: 403 (`HTTP_FORBIDDEN`);
  - quota exhaustion (429) is not raised here; it lives in `observability/limits.py`.

* It never fails open: a resolver or database error denies with 403. Every denial body carries
  a machine-readable `code` so the frontend and Conduit clients can branch on it.

The read-side callables (`effective_entitlement`, `depth_allowed`, `scopes_for_org`) live in
`resolver.py`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException

from regsuper.api.auth import Principal, get_principal
from regsuper.config import get_settings
from regsuper.entitlements.resolver import (
    EntitlementResolutionError,
    effective_entitlement,
)
from regsuper.storage.database import get_db

# --- Standard denial codes ---------------------------------------------------
# 402: payment or upgrade required (the feature is not in the tier, e.g. export or L3/L4 on
#      Lite). 403: not entitled (wrong org, missing scope, or below min_tier).
# 429: quota or rate limit exhausted, raised as QuotaExceeded(429, 'quota.exceeded') before
#      any work is done. Quotas are owned by observability/limits.py; the constant is
#      re-exported here so call sites have one source for the number.
HTTP_PAYMENT_REQUIRED = 402
HTTP_FORBIDDEN = 403
HTTP_QUOTA_EXCEEDED = 429

# Plan tier ranks (matching plan.tier 1, 2, 3 and the seeded plan codes). Used only when a
# call site passes min_tier as a plan code (e.g. min_tier='business').
_TIER_RANK = {"lite": 1, "business": 2, "advisor": 3}

# Features that deny with 403 instead of 402. Features that come with a higher plan deny with
# 402 (upgrade path). `api_access` denies with 403 because API access is a metered add-on
# granted per org, not something a customer can unlock by upgrading their plan:
#   - Lite `audit_export`: 402; Business: allowed.
#   - Lite or Business `api_access`: 403; Advisor: allowed.
_FORBIDDEN_FEATURES = frozenset({"api_access"})


def require_entitlement(
    feature: str, min_tier: str | None = None
) -> Callable[..., Awaitable[None]]:
    """Build a FastAPI dependency that gates a route on an entitlement feature.

    Typical uses: advisor multi-client routes (`feature='multi_client'`), the Conduit MCP
    surface (`feature='api_access'`), and audit export (`feature='audit_export'`,
    `min_tier='business'`).

    Args:
        feature: A boolean column on the resolved entitlement (`audit_export`, `api_access`,
            `multi_client`, `change_alerts`, `answer`).
        min_tier: Optional plan code (`'business'` or `'advisor'`) the org must meet or exceed.

    Returns:
        An async dependency. With `entitlements_enabled` off it does nothing. With it on, it
        raises 402 or 403 on denial and returns None on success; a resolver error is a 403
        (fail closed).
    """

    async def _dependency(
        principal: Principal = Depends(get_principal),  # noqa: B008 — FastAPI idiom
        session=Depends(get_db),  # noqa: B008 — FastAPI idiom
    ) -> None:
        # Flag off (the default): no resolution and no extra database read.
        if not get_settings().entitlements_enabled:
            return None

        # Fail closed: any resolution error denies with 403, never grants.
        try:
            ent = await effective_entitlement(session, principal.org_id)
        except EntitlementResolutionError as exc:
            raise HTTPException(
                status_code=HTTP_FORBIDDEN,
                detail={
                    "code": "entitlement.unresolved",
                    "feature": feature,
                    "reason": "entitlement could not be resolved (fail-closed)",
                },
            ) from exc
        except Exception as exc:  # any error is a fail-closed deny (never a grant)
            raise HTTPException(
                status_code=HTTP_FORBIDDEN,
                detail={
                    "code": "entitlement.error",
                    "feature": feature,
                    "reason": "entitlement resolver error (fail-closed)",
                },
            ) from exc

        # Check the feature column first. It is the primary and most specific denial, so Lite
        # `audit_export` gets 402 `feature.not_in_tier` even when a min_tier is also declared.
        granted = getattr(ent, feature, None)
        if granted is None:
            # An unknown feature name is a programming error surfaced as a
            # fail-closed deny rather than a silent grant.
            raise HTTPException(
                status_code=HTTP_FORBIDDEN,
                detail={
                    "code": "feature.unknown",
                    "feature": feature,
                },
            )
        if not granted:
            # The feature exists but is not in this tier. Most features deny with 402 (upgrade
            # path); those in _FORBIDDEN_FEATURES, such as `api_access`, deny with 403.
            if feature in _FORBIDDEN_FEATURES:
                raise HTTPException(
                    status_code=HTTP_FORBIDDEN,
                    detail={
                        "code": "feature.not_entitled",
                        "feature": feature,
                        "current_tier": ent.plan_code,
                    },
                )
            raise HTTPException(
                status_code=HTTP_PAYMENT_REQUIRED,
                detail={
                    "code": "feature.not_in_tier",
                    "feature": feature,
                    "current_tier": ent.plan_code,
                },
            )

        # Secondary check (403): an org that holds the feature but sits below the required plan
        # tier is still denied. Defence in depth; the feature column is the primary gate.
        if min_tier is not None:
            required = _TIER_RANK.get(min_tier)
            if required is not None and ent.tier < required:
                raise HTTPException(
                    status_code=HTTP_FORBIDDEN,
                    detail={
                        "code": "tier.below_minimum",
                        "feature": feature,
                        "min_tier": min_tier,
                        "current_tier": ent.plan_code,
                    },
                )
        return None

    return _dependency
