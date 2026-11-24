"""Entitlements: the single source of truth for an org's plan tier, scopes and analysis depth.

`effective_entitlement` resolves an org's plan, `require_entitlement` guards FastAPI routes, and
`depth_allowed` / `scopes_for_org` project the result onto a depth cap and Conduit scopes.
Enforcement is off until `entitlements_enabled` is set; quotas live in `observability/limits.py`.
"""

from __future__ import annotations

from regsuper.entitlements.guard import (
    HTTP_FORBIDDEN,
    HTTP_PAYMENT_REQUIRED,
    HTTP_QUOTA_EXCEEDED,
    require_entitlement,
)
from regsuper.entitlements.models import (
    Depth,
    EffectiveEntitlement,
    Entitlement,
)
from regsuper.entitlements.resolver import (
    depth_allowed,
    effective_entitlement,
    scopes_for_org,
)

__all__ = [
    "HTTP_FORBIDDEN",
    "HTTP_PAYMENT_REQUIRED",
    "HTTP_QUOTA_EXCEEDED",
    "Depth",
    "EffectiveEntitlement",
    "Entitlement",
    "depth_allowed",
    "effective_entitlement",
    "require_entitlement",
    "scopes_for_org",
]
