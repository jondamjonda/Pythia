"""Entitlement value object read by every tier, scope and export check.

`Entitlement` mirrors the `entitlement` table columns (`storage.models.Entitlement`) plus the
resolved `plan_code`, `tier` and `org_id`, i.e. what a guard gets after joining the org's
active subscription to its plan and entitlement. It is also exported as
`EffectiveEntitlement`; both names refer to the same class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

# Analysis depth ladder, capped by entitlement.max_depth (Lite: L2, Business and Advisor: L4).
Depth = Literal["L2", "L3", "L4"]


@dataclass(frozen=True, slots=True)
class Entitlement:
    """Resolved entitlement for one org.

    Fields mirror `storage.models.Entitlement` plus the resolved `plan_code`, `tier` and
    `org_id`. Defaults model the floor `'lite'` plan, so a default instance never grants more
    than the lowest tier. Resolution from the database lives in `resolver.py`.
    """

    org_id: UUID | None = None
    plan_code: str = "lite"
    tier: int = 1
    max_depth: Depth = "L2"
    answer_mode: str = "information"  # information|compliance
    queries_per_day: int = 10  # -1 = unlimited
    analyses_per_month: int = 3  # -1 = unlimited
    max_seats: int = 1
    audit_export: bool = False
    api_access: bool = False  # Conduit reads this
    multi_client: bool = False
    change_alerts: bool = False
    # Access to POST /api/answer is granted on every tier, so the default is True and
    # require_entitlement('answer') passes even on the 'lite' floor. The database column comes
    # from migration 009 on a fresh database and from migration 020 on an existing one.
    answer: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


# Alias so that both names import the same class.
EffectiveEntitlement = Entitlement
