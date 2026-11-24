"""AC-ENT — the C2 entitlements/quota/metering acceptance gate (T-C2c).

The CARDINAL GATE: `entitlements_enabled` may flip False→True (C2-08) ONLY after
this is green. The 9-row matrix below is the authoritative definition (docs/AC_ENT.md):

  1. Lite L2-L4 request → executes L2-only + a `layers_capped` note + 200
     (NOT a 403, NOT a crash).
  2. Business `audit_export` → 200; Lite `audit_export` → 402 (feature.not_in_tier).
  3. `api_access` → 403 for Lite/Business; 200 for Advisor.
  4. Over-quota → 429 (quota.exceeded) BEFORE any LLM spend — ZERO api_call rows.
  5. `scopes_for_org` ∅ for a non-Conduit (lite/business) org.
  6. `entitlements_enabled=False` ⇒ pure pass-through — no analysis_run/quota/billing
     rows written (byte-identical).
  7. Exactly one `billing_event` per metered event and per quota.exceeded.
  8. Quota concurrency — N parallel reserves at limit K admit exactly K (final=K).
  9. Fail-closed — a resolver/DB error denies (402/403), never silently grants; a
     provider error is swallowed (no 500).
  + AC-COST-MODEL — an L4 run rolls a non-zero cost.

DB-free rows (1,2,3,5,9-guard) run in CI without Postgres. DB-backed rows
(4,6,7,8,9-provider, AC-COST-MODEL) use the seeded Docker Postgres and SKIP cleanly
without it. The no-silent-fail-open spine is asserted throughout: 402/403/429 with a
machine `code`, never a silent grant/drop/500.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from regsuper.api.auth import Principal
from regsuper.entitlements import guard, metering
from regsuper.entitlements.guard import (
    HTTP_FORBIDDEN,
    HTTP_PAYMENT_REQUIRED,
    require_entitlement,
)
from regsuper.entitlements.models import EffectiveEntitlement
from regsuper.entitlements.resolver import EntitlementResolutionError, scopes_for_org

# --- shared tier value objects --------------------------------------------------
LITE = EffectiveEntitlement(
    plan_code="lite",
    tier=1,
    max_depth="L2",
    answer_mode="information",
    queries_per_day=10,
    analyses_per_month=3,
    audit_export=False,
    api_access=False,
)
BUSINESS = EffectiveEntitlement(
    plan_code="business",
    tier=2,
    max_depth="L4",
    answer_mode="compliance",
    queries_per_day=200,
    analyses_per_month=30,
    audit_export=True,
    api_access=False,
)
ADVISOR = EffectiveEntitlement(
    plan_code="advisor",
    tier=3,
    max_depth="L4",
    answer_mode="compliance",
    queries_per_day=-1,
    analyses_per_month=-1,
    audit_export=True,
    api_access=True,
)


def _principal() -> Principal:
    return Principal(user_id=uuid4(), org_id=uuid4(), role="owner")


def _guard_settings(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(entitlements_enabled=enabled)


def _patch_guard_resolver(monkeypatch, *, returns=None, raises=None):
    async def _fake(_session, _org_id):
        if raises is not None:
            raise raises
        return returns

    monkeypatch.setattr(guard, "effective_entitlement", _fake)


# ===========================================================================
# ROW 1 — Lite L2-L4 → L2-only + layers_capped note (NOT 403).
# ===========================================================================
def test_row1_lite_depth_clamp_is_a_note_not_403():
    executed, note = metering.clamp_layers(["L2", "L3", "L4"], LITE)
    assert executed == ["L2"]
    assert note is not None and note.startswith("layers_capped")
    executed_b, note_b = metering.clamp_layers(["L2", "L3", "L4"], BUSINESS)
    assert executed_b == ["L2", "L3", "L4"] and note_b is None


# ===========================================================================
# ROW 2 — audit_export: Lite → 402, Business → 200.
# ===========================================================================
async def test_row2_lite_export_402_business_200(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _guard_settings(True))

    _patch_guard_resolver(monkeypatch, returns=LITE)
    with pytest.raises(HTTPException) as ei:
        await require_entitlement("audit_export", min_tier="business")(
            principal=_principal(), session=object()
        )
    assert ei.value.status_code == HTTP_PAYMENT_REQUIRED
    assert ei.value.detail["code"] == "feature.not_in_tier"

    _patch_guard_resolver(monkeypatch, returns=BUSINESS)
    assert (
        await require_entitlement("audit_export", min_tier="business")(
            principal=_principal(), session=object()
        )
        is None
    )


# ===========================================================================
# ROW 3 — api_access: Lite/Business → 403, Advisor → 200.
# ===========================================================================
async def test_row3_api_access_403_for_non_advisor(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _guard_settings(True))
    for ent in (LITE, BUSINESS):
        _patch_guard_resolver(monkeypatch, returns=ent)
        with pytest.raises(HTTPException) as ei:
            await require_entitlement("api_access")(principal=_principal(), session=object())
        assert ei.value.status_code == HTTP_FORBIDDEN
        assert ei.value.detail["code"] == "feature.not_entitled"
    _patch_guard_resolver(monkeypatch, returns=ADVISOR)
    assert await require_entitlement("api_access")(principal=_principal(), session=object()) is None


# ===========================================================================
# ROW 5 — scopes_for_org ∅ for non-Conduit (lite/business), granted for advisor.
# ===========================================================================
def test_row5_scopes_empty_for_non_conduit():
    assert scopes_for_org(LITE) == set()
    assert scopes_for_org(BUSINESS) == set()
    assert scopes_for_org(ADVISOR) == {"legislation.read", "obligations.read", "answer"}


# ===========================================================================
# ROW 9a — fail-closed: a resolver error DENIES (403), never grants.
# ===========================================================================
async def test_row9_guard_fails_closed_on_resolver_error(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _guard_settings(True))
    _patch_guard_resolver(monkeypatch, raises=EntitlementResolutionError("boom"))
    with pytest.raises(HTTPException) as ei:
        await require_entitlement("audit_export")(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "entitlement.unresolved"

    _patch_guard_resolver(monkeypatch, raises=RuntimeError("db down"))
    with pytest.raises(HTTPException) as ei:
        await require_entitlement("audit_export")(principal=_principal(), session=object())
    assert ei.value.status_code == HTTP_FORBIDDEN
    assert ei.value.detail["code"] == "entitlement.error"


# ===========================================================================
# ROW 6 — entitlements_enabled=False ⇒ pure pass-through (guard returns None).
# ===========================================================================
async def test_row6_off_guard_is_pass_through(monkeypatch):
    monkeypatch.setattr(guard, "get_settings", lambda: _guard_settings(False))

    def _boom(*_a, **_k):
        raise AssertionError("resolver must NOT be consulted when OFF")

    monkeypatch.setattr(guard, "effective_entitlement", _boom)
    assert await require_entitlement("api_access")(principal=_principal(), session=object()) is None
    # And the metering helpers are inert when OFF.
    assert metering.clamp_layers(["L2", "L3", "L4"], None) == (["L2", "L3", "L4"], None)
    assert metering.answer_mode_for(None) is None


# ===========================================================================
# DB-backed rows (4, 6-rows, 7, 8, 9-provider, AC-COST-MODEL).
# ===========================================================================
async def _db_reachable() -> bool:
    from sqlalchemy import text

    from regsuper.storage.database import dispose_engine, session_scope

    await dispose_engine()
    try:
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def db_session():
    if not await _db_reachable():
        pytest.skip("no Postgres reachable (DATABASE_URL) — DB-backed AC-ENT row skipped")
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        yield session


@pytest.fixture
async def ac_org(db_session):
    from sqlalchemy import delete, text

    from regsuper.entitlements import cache
    from regsuper.storage.models import (
        AnalysisRun,
        ApiCall,
        Finding,
        Organization,
        Project,
        Subscription,
        UsageCounter,
    )

    cache.clear()
    org = Organization(name=f"ac-ent-{uuid4().hex[:8]}", org_type="sme")
    db_session.add(org)
    await db_session.flush()
    org_id = org.id
    try:
        yield org_id
    finally:
        await db_session.execute(delete(ApiCall).where(ApiCall.org_id == org_id))
        await db_session.execute(delete(Finding).where(Finding.org_id == org_id))
        await db_session.execute(delete(UsageCounter).where(UsageCounter.org_id == org_id))
        await db_session.execute(
            text("ALTER TABLE billing_event DISABLE TRIGGER trg_billing_event_no_update")
        )
        await db_session.execute(
            text("DELETE FROM billing_event WHERE org_id = :oid"), {"oid": str(org_id)}
        )
        await db_session.execute(
            text("ALTER TABLE billing_event ENABLE TRIGGER trg_billing_event_no_update")
        )
        await db_session.execute(delete(AnalysisRun).where(AnalysisRun.org_id == org_id))
        await db_session.execute(delete(Project).where(Project.org_id == org_id))
        await db_session.execute(delete(Subscription).where(Subscription.org_id == org_id))
        await db_session.execute(
            text("DELETE FROM organization WHERE id = :oid"), {"oid": str(org_id)}
        )
        await db_session.flush()
        cache.clear()


# --------------------------------------------------------------------------- ROW 4 — 429-before-spend
async def test_row4_over_quota_429_before_spend(db_session, ac_org):
    """Lite at analyses_per_month=3: the 4th reserve → 429 quota.exceeded, raised
    BEFORE any LLM spend (the reserve is the gate). Exactly one quota.exceeded
    billing_event is coupled to the 429 (row 7)."""
    from sqlalchemy import func, select

    from regsuper.observability.limits import QuotaExceeded, check_and_reserve_quota
    from regsuper.storage.models import BillingEvent as BillingEventRow

    for _ in range(3):
        await check_and_reserve_quota(db_session, ac_org, "analyses", limit=3)
    with pytest.raises(QuotaExceeded) as ei:
        await check_and_reserve_quota(db_session, ac_org, "analyses", limit=3)
    assert ei.value.status_code == 429
    assert ei.value.code == "quota.exceeded"
    # Exactly one coupled quota.exceeded billing_event (row 7).
    n_be = (
        await db_session.execute(
            select(func.count(BillingEventRow.id)).where(
                BillingEventRow.org_id == ac_org, BillingEventRow.event_type == "quota.exceeded"
            )
        )
    ).scalar_one()
    assert n_be == 1


# --------------------------------------------------------------------------- ROW 7 — exactly one metered billing_event
async def test_row7_one_billing_event_per_metered_event(db_session, ac_org):
    from sqlalchemy import func, select

    from regsuper.billing.events import emit_billing_event
    from regsuper.storage.models import BillingEvent as BillingEventRow

    await emit_billing_event(db_session, ac_org, "analysis.metered", payload={"tokens_input": 10})
    await db_session.flush()
    n = (
        await db_session.execute(
            select(func.count(BillingEventRow.id)).where(
                BillingEventRow.org_id == ac_org, BillingEventRow.event_type == "analysis.metered"
            )
        )
    ).scalar_one()
    assert n == 1


# --------------------------------------------------------------------------- ROW 8 — quota concurrency
async def test_row8_quota_concurrency_admits_exactly_K(db_session, ac_org):
    """10 genuinely-parallel reserves at limit 3 → exactly 3 succeed, 7 get 429,
    final count = 3 (over-quota reserves refund). Runs each reserve on a SEPARATE
    connection so the atomic UPSERT is exercised; the org is committed first so the
    sibling connections can see it."""
    from sqlalchemy import select

    from regsuper.observability.limits import (
        QuotaExceeded,
        check_and_reserve_quota,
        period_start,
    )
    from regsuper.storage.database import session_scope
    from regsuper.storage.models import UsageCounter

    await db_session.commit()  # commit the org so sibling connections see it
    LIMIT, N = 3, 10

    async def _one() -> bool:
        async with session_scope() as s:
            try:
                await check_and_reserve_quota(s, ac_org, "analyses", limit=LIMIT)
                return True
            except QuotaExceeded:
                return False

    results = await asyncio.gather(*[_one() for _ in range(N)])
    assert sum(results) == LIMIT
    assert results.count(False) == N - LIMIT
    async with session_scope() as s:
        cnt = (
            await s.execute(
                select(UsageCounter.count).where(
                    UsageCounter.org_id == ac_org,
                    UsageCounter.metric == "analyses",
                    UsageCounter.period_start == period_start("analyses"),
                )
            )
        ).scalar_one()
    assert int(cnt) == LIMIT


# --------------------------------------------------------------------------- ROW 9b — provider error swallowed
async def test_row9_provider_error_is_swallowed(db_session, ac_org):
    """A billing provider that raises is SWALLOWED (no 500); the durable row was
    already written before the provider call, so no billing signal is lost."""
    from sqlalchemy import func, select

    from regsuper.billing import events as billing_events
    from regsuper.storage.models import BillingEvent as BillingEventRow

    class _Boom:
        async def handle(self, _event):
            raise RuntimeError("payment provider outage")

    with patch.object(billing_events, "_provider", lambda: _Boom()):
        ev = await billing_events.emit_billing_event(
            db_session, ac_org, "analysis.metered", payload={}
        )
    assert ev.event_type == "analysis.metered"  # returned despite the provider blowing up
    n = (
        await db_session.execute(
            select(func.count(BillingEventRow.id)).where(
                BillingEventRow.org_id == ac_org, BillingEventRow.event_type == "analysis.metered"
            )
        )
    ).scalar_one()
    assert n == 1  # the durable row landed before the (failed) provider call


# --------------------------------------------------------------------------- ROW 6 (DB) — OFF writes nothing
async def test_row6_off_writes_no_metering_rows(db_session, ac_org):
    """entitlements_enabled=False ⇒ resolve_for_entry returns None and the run-row /
    reserve helpers are no-ops — no analysis_run / usage_counter / billing_event
    rows are written (byte-identical)."""
    from sqlalchemy import func, select

    from regsuper.storage.models import AnalysisRun, BillingEvent, UsageCounter

    off = SimpleNamespace(entitlements_enabled=False)
    with patch.object(metering, "get_settings", lambda: off):
        ent = await metering.resolve_for_entry(ac_org, session=db_session)
        assert ent is None
        run_id = await metering.open_analysis_run(
            db_session,
            org_id=ac_org,
            project_id=None,
            entitlement=ent,
            requested_layers=["L2"],
            executed_layers=["L2"],
        )
        assert run_id is None
        reserved = await metering.reserve_for_run(
            db_session,
            org_id=ac_org,
            entitlement=ent,
            executed_layers=["L2"],
        )
        assert reserved == []
    await db_session.flush()
    for model in (AnalysisRun, UsageCounter, BillingEvent):
        n = (
            await db_session.execute(
                select(func.count()).select_from(model).where(model.org_id == ac_org)
            )
        ).scalar_one()
        assert n == 0, f"{model.__name__} should have zero rows when OFF"


# --------------------------------------------------------------------------- AC-COST-MODEL — non-zero L4 cost
async def test_ac_cost_model_l4_run_rolls_non_zero_cost(db_session, ac_org):
    """An L4 run's cost_usd rolls non-zero from its tagged api_call rows (the L2/L3
    LLM spend) — the cost model is wired through the run roll-up."""
    from regsuper.entitlements import metering as met
    from regsuper.entitlements.models import EffectiveEntitlement as Ent
    from regsuper.storage.models import AnalysisRun, ApiCall

    ent = Ent(org_id=ac_org, plan_code="business", tier=2, max_depth="L4")
    run_id = await met.open_analysis_run(
        db_session,
        org_id=ac_org,
        project_id=None,
        entitlement=ent,
        requested_layers=["L2", "L3", "L4"],
        executed_layers=["L2", "L3", "L4"],
    )
    db_session.add(
        ApiCall(
            provider="openai",
            model="gpt-4o",
            endpoint="chat.completions",
            source="analysis.l3",
            org_id=ac_org,
            analysis_run_id=run_id,
            tokens_input=100,
            tokens_output=50,
            tokens_cached=0,
            cost_usd=0.2,
            latency_ms=1,
            status="ok",
            request_json={},
            response_json={},
            response_summary="",
        )
    )
    await db_session.flush()
    await met.finish_analysis_run(
        db_session, run_id, status="done", executed_layers=["L2", "L3", "L4"]
    )
    run = await db_session.get(AnalysisRun, run_id)
    assert float(run.cost_usd) > 0.0
