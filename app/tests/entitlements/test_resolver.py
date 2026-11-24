"""ENT-1 — effective_entitlement: floor-resolve, subscription resolve, expired→
floor, fail-closed-deny, 60s TTL re-resolve (WP-044 / T-C2a).

DB-backed rows (floor + subscription tiers) run against the seeded Docker
Postgres and skip cleanly without it. The fail-closed and cache/TTL mechanics are
DB-free (a mocked AsyncSession).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from regsuper.entitlements import cache
from regsuper.entitlements.resolver import (
    EntitlementResolutionError,
    _resolve_floor,
    effective_entitlement,
)

from .conftest import ADVISOR_PLAN, BUSINESS_PLAN


# ---------------------------------------------------------------------------
# DB-backed: floor + subscription resolution against the seeded price book.
# ---------------------------------------------------------------------------
async def test_no_subscription_resolves_to_lite_floor(db_session, second_org):
    """An org with no current subscription resolves to the seeded `lite` floor —
    read FROM THE DB, not a hardcoded dataclass."""
    ent = await effective_entitlement(db_session, second_org)
    assert ent.plan_code == "lite"
    assert ent.tier == 1
    assert ent.max_depth == "L2"
    assert ent.audit_export is False
    assert ent.api_access is False
    assert ent.analyses_per_month == 3
    assert ent.queries_per_day == 10


async def test_active_business_subscription_resolves_to_business(db_session, second_org):
    from regsuper.storage.models import Subscription

    db_session.add(Subscription(org_id=second_org, plan_id=BUSINESS_PLAN, status="active"))
    await db_session.flush()
    cache.invalidate(second_org)

    ent = await effective_entitlement(db_session, second_org)
    assert ent.plan_code == "business"
    assert ent.tier == 2
    assert ent.max_depth == "L4"
    assert ent.audit_export is True
    assert ent.api_access is False
    assert ent.analyses_per_month == 30


async def test_active_advisor_subscription_resolves_to_advisor(db_session, second_org):
    from regsuper.storage.models import Subscription

    db_session.add(Subscription(org_id=second_org, plan_id=ADVISOR_PLAN, status="active"))
    await db_session.flush()
    cache.invalidate(second_org)

    ent = await effective_entitlement(db_session, second_org)
    assert ent.plan_code == "advisor"
    assert ent.tier == 3
    assert ent.api_access is True
    assert ent.multi_client is True
    assert ent.analyses_per_month == -1  # unlimited


async def test_answer_feature_is_true_for_every_tier(db_session, second_org):
    """The `answer` feature is TRUE on the floor + every seeded tier — answering
    is universal (depth/quota differentiate, not whether you may ask). This is the
    column staged migration 020 / the 009 seed add; without it the answer endpoint
    guard would 403 feature.unknown when entitlements_enabled flips ON."""
    from regsuper.storage.models import Subscription

    # Floor (no subscription) → lite, answer True.
    floor = await effective_entitlement(db_session, second_org)
    assert floor.plan_code == "lite"
    assert floor.answer is True

    # Each of the three seeded tiers carries answer True.
    for plan_id, expected_code in (
        (BUSINESS_PLAN, "business"),
        (ADVISOR_PLAN, "advisor"),
    ):
        # Replace the (possibly-existing) current subscription with this tier.
        from sqlalchemy import delete

        await db_session.execute(delete(Subscription).where(Subscription.org_id == second_org))
        db_session.add(Subscription(org_id=second_org, plan_id=plan_id, status="active"))
        await db_session.flush()
        cache.invalidate(second_org)

        ent = await effective_entitlement(db_session, second_org)
        assert ent.plan_code == expected_code
        assert ent.answer is True


async def test_canceled_subscription_falls_back_to_lite(db_session, second_org):
    """A canceled (or non-active-status) subscription does NOT entitle → lite floor."""
    from regsuper.storage.models import Subscription

    db_session.add(Subscription(org_id=second_org, plan_id=BUSINESS_PLAN, status="canceled"))
    await db_session.flush()
    cache.invalidate(second_org)

    ent = await effective_entitlement(db_session, second_org)
    assert ent.plan_code == "lite"


async def test_expired_subscription_falls_back_to_lite(db_session, second_org):
    """A subscription with valid_to set (no longer current) → lite floor."""
    from regsuper.storage.models import Subscription

    past = datetime.now(UTC) - timedelta(days=1)
    db_session.add(
        Subscription(
            org_id=second_org,
            plan_id=BUSINESS_PLAN,
            status="active",
            valid_to=past,
        )
    )
    await db_session.flush()
    cache.invalidate(second_org)

    ent = await effective_entitlement(db_session, second_org)
    assert ent.plan_code == "lite"


async def test_resolve_floor_reads_from_db(db_session):
    """_resolve_floor returns the seeded lite entitlement (price-book read)."""
    org = uuid4()
    ent = await _resolve_floor(db_session, org)
    assert ent.plan_code == "lite"
    assert ent.org_id == org


# ---------------------------------------------------------------------------
# DB-free: FAIL CLOSED + TTL cache mechanics.
# ---------------------------------------------------------------------------
class _BoomSession:
    """An AsyncSession whose every execute raises — exercises fail-closed."""

    async def execute(self, *_a, **_k):
        raise RuntimeError("simulated DB outage")


async def test_resolver_db_error_raises_never_grants():
    """A resolver/DB error RAISES (fail-closed) — it never returns a default grant."""
    cache.clear()
    # The error PROPAGATES (the guard maps it to a 403 deny) — it must NEVER be
    # swallowed into a default grant. RuntimeError is what _BoomSession raises.
    with pytest.raises(RuntimeError):
        await effective_entitlement(_BoomSession(), uuid4())


async def test_missing_floor_plan_raises_resolution_error():
    """If the org has no subscription AND the floor plan row is absent, the
    resolver raises EntitlementResolutionError (a corrupt price book is an error,
    never an implicit grant)."""

    class _EmptySession:
        async def execute(self, *_a, **_k):
            class _R:
                def first(self_inner):
                    return None  # no subscription row AND no floor row

            return _R()

    cache.clear()
    with pytest.raises(EntitlementResolutionError):
        await effective_entitlement(_EmptySession(), uuid4())


async def test_cache_hit_skips_db_within_ttl():
    """A 2nd call within 60s is served from cache — NO DB round-trip."""
    from regsuper.entitlements.models import EffectiveEntitlement

    org = uuid4()
    seeded = EffectiveEntitlement(org_id=org, plan_code="business", tier=2, max_depth="L4")
    cache.put(org, seeded)

    calls = {"n": 0}

    class _CountingSession:
        async def execute(self, *_a, **_k):
            calls["n"] += 1
            raise AssertionError("DB should not be hit on a cache hit")

    ent = await effective_entitlement(_CountingSession(), org)
    assert ent.plan_code == "business"
    assert calls["n"] == 0


async def test_ttl_expiry_forces_reresolve():
    """Past the 60s TTL the entry is a miss → the resolver re-reads."""
    from regsuper.entitlements.models import EffectiveEntitlement

    org = uuid4()
    seeded = EffectiveEntitlement(org_id=org, plan_code="advisor", tier=3)
    # Put with an already-expired entry (ttl in the past).
    cache.put(org, seeded, now=0.0, ttl=60.0)
    # get() at t=120 is past expiry → miss.
    assert cache.get(org, now=120.0) is None
