"""ENT-4 — require_entitlement dependency: OFF=pass-through, 402/403 matrix,
fail-closed-deny (WP-044/T-C2a).

DB-free: the dependency's `session`/`principal` are injected directly and
`effective_entitlement` + `get_settings` are monkeypatched, so the 402/403/None
decision logic is exercised without Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from regsuper.api.auth import Principal
from regsuper.entitlements import guard
from regsuper.entitlements.guard import (
    HTTP_FORBIDDEN,
    HTTP_PAYMENT_REQUIRED,
    require_entitlement,
)
from regsuper.entitlements.models import EffectiveEntitlement
from regsuper.entitlements.resolver import EntitlementResolutionError


def _principal() -> Principal:
    return Principal(user_id=uuid4(), org_id=uuid4(), role="owner")


def _settings(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(entitlements_enabled=enabled)


def _patch_resolver(monkeypatch, *, returns=None, raises=None):
    async def _fake(_session, _org_id):
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(guard, "effective_entitlement", _fake)


# ---------------------------------------------------------------------------
# OFF — pure pass-through (the cardinal default-OFF byte-identical property).
# ---------------------------------------------------------------------------
async def test_off_is_pure_pass_through(monkeypatch):
    """entitlements_enabled=False → the dependency returns None, no resolve,
    regardless of feature/tier."""
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(False))

    def _boom(*_a, **_k):
        raise AssertionError("resolver must NOT be consulted when OFF")

    monkeypatch.setattr(guard, "effective_entitlement", _boom)

    dep = require_entitlement("audit_export", min_tier="business")
    result = await dep(principal=_principal(), session=object())
    assert result is None


# ---------------------------------------------------------------------------
# ON — the 402/403 matrix.
# ---------------------------------------------------------------------------
async def test_lite_export_is_402(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code="lite", tier=1, audit_export=False),
    )
    dep = require_entitlement("audit_export", min_tier="business")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_PAYMENT_REQUIRED
    # 402 comes from the min_tier check OR the feature check; either way the
    # detail carries a machine code (no silent fail).
    assert isinstance(ei.value.detail, dict)
    assert "code" in ei.value.detail


async def test_business_export_passes(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code="business", tier=2, audit_export=True),
    )
    dep = require_entitlement("audit_export", min_tier="business")
    assert await dep(principal=_principal(), session=object()) is None


async def test_lite_api_access_is_403(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code="lite", tier=1, api_access=False),
    )
    dep = require_entitlement("api_access")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "feature.not_entitled"


async def test_business_api_access_is_403(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code="business", tier=2, api_access=False),
    )
    dep = require_entitlement("api_access")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN


async def test_advisor_api_access_passes(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code="advisor", tier=3, api_access=True),
    )
    dep = require_entitlement("api_access")
    assert await dep(principal=_principal(), session=object()) is None


@pytest.mark.parametrize(
    ("plan_code", "tier"),
    [("lite", 1), ("business", 2), ("advisor", 3)],
)
async def test_answer_feature_passes_for_every_tier(monkeypatch, plan_code, tier):
    """require_entitlement('answer') PASSES (returns None, no 402/403) for every
    tier when entitlements_enabled=True. Answering is universal; the `answer`
    feature defaults TRUE on the floor + all three seeded tiers. Without the
    column the guard would 403 feature.unknown (getattr -> None)."""
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code=plan_code, tier=tier, answer=True),
    )
    dep = require_entitlement("answer")
    assert await dep(principal=_principal(), session=object()) is None


async def test_answer_feature_floor_default_is_true():
    """The in-memory EffectiveEntitlement floor (no args) carries answer=True, so a
    fallback/floor instance passes the answer guard."""
    assert EffectiveEntitlement().answer is True


async def test_min_tier_below_minimum_is_403(monkeypatch):
    """A feature the tier HAS, but below min_tier → 403 tier.below_minimum."""
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    # An org that has audit_export True but tier 1 (hypothetical) and min_tier business.
    _patch_resolver(
        monkeypatch,
        returns=EffectiveEntitlement(plan_code="lite", tier=1, audit_export=True),
    )
    dep = require_entitlement("audit_export", min_tier="business")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "tier.below_minimum"


# ---------------------------------------------------------------------------
# Fail-closed — a resolver error DENIES (403), never a silent grant.
# ---------------------------------------------------------------------------
async def test_resolution_error_denies_403(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(monkeypatch, raises=EntitlementResolutionError("missing floor"))
    dep = require_entitlement("audit_export")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "entitlement.unresolved"


async def test_generic_resolver_error_denies_403(monkeypatch):
    """Any unexpected exception inside the resolver is a fail-closed deny."""
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(monkeypatch, raises=RuntimeError("db outage"))
    dep = require_entitlement("audit_export")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "entitlement.error"


async def test_unknown_feature_denies_403(monkeypatch):
    """An unknown feature name is a fail-closed deny, never a silent grant."""
    monkeypatch.setattr(guard, "get_settings", lambda: _settings(True))
    _patch_resolver(monkeypatch, returns=EffectiveEntitlement(plan_code="advisor", tier=3))
    dep = require_entitlement("not_a_real_feature")
    with pytest.raises(HTTPException) as ei:
        await dep(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "feature.unknown"
