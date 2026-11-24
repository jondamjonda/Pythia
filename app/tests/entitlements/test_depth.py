"""ENT-2 — depth_allowed returns the entitlement's max_depth cap (WP-044/T-C2a).

Pure projection; DB-free. The clamp-as-NOTE itself lives at the entry point
(ENT-5, T-C2c); here we only assert the cap returned per tier.
"""

from __future__ import annotations

import pytest

from regsuper.entitlements.models import EffectiveEntitlement
from regsuper.entitlements.resolver import depth_allowed


def _ent(plan_code: str, max_depth: str, tier: int) -> EffectiveEntitlement:
    return EffectiveEntitlement(plan_code=plan_code, max_depth=max_depth, tier=tier)


def test_lite_caps_at_l2():
    assert depth_allowed(_ent("lite", "L2", 1)) == "L2"


def test_business_caps_at_l4():
    assert depth_allowed(_ent("business", "L4", 2)) == "L4"


def test_advisor_caps_at_l4():
    assert depth_allowed(_ent("advisor", "L4", 3)) == "L4"


def test_depth_allowed_accepts_duck_typed_entitlement():
    """Anything carrying the entitlement fields is accepted (a test stub)."""

    class _Stub:
        max_depth = "L3"
        api_access = False

    assert depth_allowed(_Stub()) == "L3"


def test_depth_allowed_rejects_bare_org_id():
    """A bare org id (not a resolved entitlement) raises — resolution is the
    entry point's job, never inferred here."""
    from uuid import uuid4

    with pytest.raises(TypeError):
        depth_allowed(uuid4())
