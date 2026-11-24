"""ENT-5 (T-C2c) — per-tier clamps + quota-before-spend at /analyze.

  * clamp_layers is CLAMP-as-NOTE: Lite (cap L2) asking [L2,L3,L4] runs [L2] +
    a layers_capped note + 200 — NEVER a 403/crash. Business/Advisor (cap L4) run
    everything (no note).
  * the wired project_analyze path executes only the allowed layers, surfaces the
    note, snapshots the tier answer_mode onto the run row, and still returns 200.
  * 429-BEFORE-SPEND: an over-quota analyze raises QuotaExceeded(429) BEFORE the
    cascade — assert ZERO api_call rows for that request (AC-ENT row 4) and the run
    row marked 'refused'.
  * OFF ⇒ no clamp, no note, no run row (byte-identical).

clamp_layers / answer_mode_for are DB-free pure-function tests. The wired-path
tests are DB-backed (the seeded Postgres) and invoke project_analyze directly so
all session_scope calls share this test's loop; the cascade is patched (no LLM).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import func, select

from regsuper.api import projects as projects_api
from regsuper.api.projects import AnalyzeRequest
from regsuper.entitlements import metering
from regsuper.entitlements.models import EffectiveEntitlement
from regsuper.storage.models import AnalysisRun, ApiCall, Project
from tests.api.conftest import LITE_PLAN, subscribe


# =========================================================================== pure clamp_layers
def _ent(plan_code: str, max_depth: str, *, analyses_per_month: int = 3) -> EffectiveEntitlement:
    return EffectiveEntitlement(
        org_id=None,
        plan_code=plan_code,
        tier={"lite": 1, "business": 2, "advisor": 3}[plan_code],
        max_depth=max_depth,
        answer_mode="information" if plan_code == "lite" else "compliance",
        analyses_per_month=analyses_per_month,
    )


def test_clamp_lite_L4_to_L2_as_note():
    """Lite (cap L2) asking [L2,L3,L4] → executes [L2] + a layers_capped note."""
    executed, note = metering.clamp_layers(["L2", "L3", "L4"], _ent("lite", "L2"))
    assert executed == ["L2"]
    assert note is not None and note.startswith("layers_capped")
    assert "L3" in note and "L4" in note


def test_clamp_business_L4_unchanged_no_note():
    """Business (cap L4) asking everything → executes everything, no note."""
    executed, note = metering.clamp_layers(["L2", "L3", "L4"], _ent("business", "L4"))
    assert executed == ["L2", "L3", "L4"]
    assert note is None


def test_clamp_preserves_order_and_drops_only_above_cap():
    executed, note = metering.clamp_layers(["L2", "L3"], _ent("lite", "L2"))
    assert executed == ["L2"]
    assert note is not None and "L3" in note


def test_clamp_none_entitlement_is_passthrough():
    """entitlement=None (entitlements OFF) → requested list returned unchanged, no note."""
    executed, note = metering.clamp_layers(["L2", "L3", "L4"], None)
    assert executed == ["L2", "L3", "L4"]
    assert note is None


def test_answer_mode_for():
    assert metering.answer_mode_for(_ent("lite", "L2")) == "information"
    assert metering.answer_mode_for(_ent("business", "L4")) == "compliance"
    assert metering.answer_mode_for(None) is None


# =========================================================================== wired-path helpers
def _settings(*, entitlements_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        law_source="regsuper",
        compliance_guard_enabled=False,
        compliance_guard_mode="shadow",
        entity_scoping_enabled=False,
        entitlements_enabled=entitlements_enabled,
    )


def _l2_report() -> SimpleNamespace:
    return SimpleNamespace(
        n_candidates=0,
        n_evaluated=0,
        n_skipped_cached=0,
        n_applicable=0,
        n_ambiguous=0,
        n_not_applicable=0,
        n_refused=0,
        n_errors=0,
        findings=[],
        sample_errors=[],
        total_latency_ms=0,
    )


async def _make_project(org_id: UUID) -> UUID:
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        project = Project(
            org_id=org_id,
            name=f"clamp-proj-{uuid4().hex[:6]}",
            sector="general",
            jurisdiction_scope=["CZ"],
            entity_type=None,
            analysis_depth="standard",
            metadata_={},
        )
        session.add(project)
        await session.flush()
        return project.id


async def _emit(*_a, **_k):
    return None


async def _health():
    return {}


# =========================================================================== wired clamp (Lite L4 → L2 + note + 200)
async def test_wired_lite_L4_request_runs_L2_with_note_not_403(metering_org, db_session):
    """Lite org requesting [L2,L3,L4]: the cascade runs ONLY L2, the response carries
    the layers_capped note + executed layers, returns 200 (NOT 403/crash), and the
    run row snapshots tier='lite'/answer_mode='information'."""
    org_id = metering_org
    await subscribe(db_session, org_id, LITE_PLAN)  # explicit lite (or floor); cap = L2
    await db_session.commit()
    pid = await _make_project(org_id)
    ran: dict[str, bool] = {}

    async def fake_l2(project_id, *, max_concurrency, limit, force, as_of, analysis_run_id=None):
        ran["L2"] = True
        return _l2_report()

    async def fake_l3(*_a, **_k):
        ran["L3"] = True  # must NOT run
        raise AssertionError("L3 ran under a Lite clamp")

    on = _settings(entitlements_enabled=True)
    with (
        patch.object(projects_api, "get_settings", lambda: on),
        patch.object(projects_api.metering, "get_settings", lambda: on),
        patch.object(projects_api, "run_l2", fake_l2),
        patch.object(projects_api, "run_l3", fake_l3),
        patch.object(projects_api, "emit", _emit),
        patch.object(projects_api.call_stats, "citation_health", _health),
    ):
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2", "L3", "L4"]))

    assert resp.layers == ["L2"]  # executed = clamped
    assert ran.get("L2") is True and "L3" not in ran  # only L2 ran
    assert resp.notes and resp.notes[0].startswith("layers_capped")
    run = (
        (await db_session.execute(select(AnalysisRun).where(AnalysisRun.org_id == org_id)))
        .scalars()
        .one()
    )
    assert run.tier == "lite"
    assert run.answer_mode == "information"
    assert run.requested_depth == "L4"  # requested snapshot keeps the asked-for depth
    assert run.executed_layers == ["L2"]


# =========================================================================== 429-before-spend (zero api_call rows)
async def test_over_quota_429_before_spend_zero_apicalls(metering_org, db_session):
    """A Lite org already at analyses_per_month uses up its quota → the next analyze
    raises QuotaExceeded(429) BEFORE the cascade: the cascade NEVER runs, ZERO
    api_call rows are written for the request (AC-ENT row 4), and the run row is
    marked 'refused'. The coupled quota.exceeded billing_event is emitted inside the
    reserve (not double-emitted)."""
    from fastapi import HTTPException

    from regsuper.observability import limits

    org_id = metering_org
    await subscribe(db_session, org_id, LITE_PLAN)  # analyses_per_month = 3
    # Pre-consume the entire monthly analyses allowance (reserve 3 → count=3=limit).
    for _ in range(3):
        await limits.check_and_reserve_quota(db_session, org_id, "analyses", limit=3)
    await db_session.commit()
    pid = await _make_project(org_id)
    ran: dict[str, bool] = {}

    async def fake_l2(project_id, *, max_concurrency, limit, force, as_of, analysis_run_id=None):
        ran["L2"] = True  # must NOT run — the reserve raises before this
        return _l2_report()

    on = _settings(entitlements_enabled=True)
    with (
        patch.object(projects_api, "get_settings", lambda: on),
        patch.object(projects_api.metering, "get_settings", lambda: on),
        patch.object(projects_api, "run_l2", fake_l2),
        patch.object(projects_api, "emit", _emit),
        patch.object(projects_api.call_stats, "citation_health", _health),
    ):
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2"]))

    # project_analyze returns a JSONResponse(429) (not a raised HTTPException) — the
    # caller surfaces the explicit status + machine code.
    assert resp.status_code == 429
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert body["detail"]["code"] == "quota.exceeded"
    assert body["detail"]["metric"] == "analyses"
    assert "L2" not in ran  # the cascade never started
    # ZERO api_call rows for this org's metered request (nothing was spent).
    n_apicalls = (
        await db_session.execute(select(func.count(ApiCall.id)).where(ApiCall.org_id == org_id))
    ).scalar_one()
    assert n_apicalls == 0
    # The run row exists and is 'refused' (auditable), and the counter is still 3
    # (the over-quota reserve refunded itself).
    run = (
        (await db_session.execute(select(AnalysisRun).where(AnalysisRun.org_id == org_id)))
        .scalars()
        .one()
    )
    assert run.status == "refused"
    from regsuper.observability.limits import period_start
    from regsuper.storage.models import UsageCounter

    cnt = (
        await db_session.execute(
            select(UsageCounter.count).where(
                UsageCounter.org_id == org_id,
                UsageCounter.metric == "analyses",
                UsageCounter.period_start == period_start("analyses"),
            )
        )
    ).scalar_one()
    assert int(cnt) == 3  # refunded the over-quota reserve → only the admitted 3 stand
    _ = HTTPException  # imported for documentation of the contrast (we DON'T raise here)
