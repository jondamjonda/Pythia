"""BIL-2 — on_subscription_changed invalidates the entitlement cache so a plan
change is reflected within the 60s TTL window (WP-044/T-C2a, the billing↔
entitlements coupling). DB-free.
"""

from __future__ import annotations

from uuid import uuid4

from regsuper.billing.events import on_subscription_changed
from regsuper.entitlements import cache
from regsuper.entitlements.models import EffectiveEntitlement


async def test_on_subscription_changed_drops_the_cached_entry():
    org = uuid4()
    cache.put(org, EffectiveEntitlement(org_id=org, plan_code="lite", tier=1))
    assert cache.get(org) is not None

    await on_subscription_changed(session=None, org_id=org)
    assert cache.get(org) is None


async def test_invalidate_is_idempotent_for_absent_org():
    """Invalidating an org with no cached entry is a clean no-op (no raise)."""
    await on_subscription_changed(session=None, org_id=uuid4())  # must not raise


async def test_invalidate_only_drops_the_named_org():
    a, b = uuid4(), uuid4()
    cache.put(a, EffectiveEntitlement(org_id=a, plan_code="business", tier=2))
    cache.put(b, EffectiveEntitlement(org_id=b, plan_code="advisor", tier=3))

    await on_subscription_changed(session=None, org_id=a)
    assert cache.get(a) is None
    assert cache.get(b) is not None  # b untouched


async def test_next_resolve_after_invalidation_reflects_new_plan(monkeypatch):
    """After on_subscription_changed, the next effective_entitlement re-resolves
    (no 60s staleness) — proven by a stub session that returns the NEW plan."""
    from regsuper.entitlements import resolver
    from regsuper.entitlements.models import EffectiveEntitlement as E

    org = uuid4()
    # Seed the cache with the OLD (lite) plan.
    cache.put(org, E(org_id=org, plan_code="lite", tier=1))

    # A resolver stub that, when actually consulted, returns the NEW (business) plan.
    async def _fake_resolve_via_db(_session, _org):
        return E(org_id=org, plan_code="business", tier=2, max_depth="L4")

    # Cache hit first → still lite.
    assert (await resolver.effective_entitlement(_DummySession(), org)).plan_code == "lite"

    # Invalidate, then the next resolve must re-read (we route through a patched
    # internal so no DB is needed).
    await on_subscription_changed(session=None, org_id=org)
    monkeypatch.setattr(resolver, "_resolve_floor", _fake_resolve_via_db)

    # With the cache cleared and no subscription rows in the stub session, the
    # resolver falls through to _resolve_floor (patched to the new plan).
    ent = await resolver.effective_entitlement(_NoSubSession(), org)
    assert ent.plan_code == "business"


class _DummySession:
    async def execute(self, *_a, **_k):  # never reached on a cache hit
        raise AssertionError("cache hit should short-circuit")


class _NoSubSession:
    """A session whose subscription lookup returns no row (so the resolver falls
    through to the floor, which the test patches)."""

    async def execute(self, *_a, **_k):
        class _R:
            def first(self_inner):
                return None

        return _R()
