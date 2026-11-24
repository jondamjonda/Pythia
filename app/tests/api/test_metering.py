"""MET-1 (T-C2c) — metering write-through on /analyze.

Proves the run-row lifecycle + the per-run cost roll-up:
  * entitlements_enabled=False ⇒ NO analysis_run row is written (byte-identical).
  * entitlements_enabled=True ⇒ exactly ONE analysis_run row per analyze, carrying
    the real org_id (NOT NULL), the tier snapshot, the tier's answer_mode, and
    cost_usd = SUM(api_call.cost_usd WHERE analysis_run_id=run) over the rows the
    cascade tagged with the threaded run_id.
  * AC-COST-MODEL: an L4 run rolls a non-zero cost.
  * finish_analysis_run's roll-up is unit-tested directly against tagged api_call
    rows.

DB-backed (the seeded Docker Postgres). project_analyze is invoked DIRECTLY as a
coroutine (not via TestClient) so every session_scope it opens shares this test's
event loop + engine — and the cascade runners are PATCHED to write real api_call
rows tagged with the threaded analysis_run_id, so no live LLM runs. The org_id
discipline (b3-06a bug class) is asserted: every metering write carries the real
principal org.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from regsuper.api import projects as projects_api
from regsuper.api.projects import AnalyzeRequest
from regsuper.storage.models import AnalysisRun, ApiCall, Project
from tests.api.conftest import BUSINESS_PLAN, subscribe


def _settings(*, entitlements_enabled: bool) -> SimpleNamespace:
    """A minimal settings namespace project_analyze + metering read (getattr-safe)."""
    return SimpleNamespace(
        law_source="regsuper",
        compliance_guard_enabled=False,
        compliance_guard_mode="shadow",
        entity_scoping_enabled=False,
        entitlements_enabled=entitlements_enabled,
    )


def _fake_l2_report() -> SimpleNamespace:
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


def _fake_l4_report() -> SimpleNamespace:
    return SimpleNamespace(
        n_obligations_scanned=0,
        n_obligations_with_evidence=0,
        n_gaps_persisted=0,
        n_evidence_covered=0,
        n_client_documents=0,
        sample_errors=[],
        total_latency_ms=0,
    )


async def _make_project(org_id: UUID) -> UUID:
    """Persist a real project owned by ``org_id`` and return its id (own session)."""
    from regsuper.storage.database import session_scope

    async with session_scope() as session:
        project = Project(
            org_id=org_id,
            name=f"meter-proj-{uuid4().hex[:6]}",
            sector="general",
            jurisdiction_scope=["CZ"],
            entity_type=None,
            analysis_depth="standard",
            metadata_={},
        )
        session.add(project)
        await session.flush()
        return project.id


def _spy_l2_writes_apicall(org_id: UUID, cost: float):
    """A run_l2 fake that writes one real api_call row tagged with the threaded
    analysis_run_id (so the run-row roll-up has something to sum). It asserts the
    run_id is threaded through (the MET-1 contract)."""

    async def fake_l2(project_id, *, max_concurrency, limit, force, as_of, analysis_run_id=None):
        assert analysis_run_id is not None, "MET-1: run_id must be threaded into the cascade"
        from regsuper.storage.database import session_scope

        async with session_scope() as session:
            session.add(
                ApiCall(
                    provider="openai",
                    model="gpt-4o-mini",
                    endpoint="chat.completions",
                    source="analysis.l2",
                    project_id=project_id,
                    org_id=org_id,
                    analysis_run_id=analysis_run_id,
                    tokens_input=10,
                    tokens_output=5,
                    tokens_cached=0,
                    cost_usd=cost,
                    latency_ms=1,
                    status="ok",
                    request_json={},
                    response_json={},
                    response_summary="",
                )
            )
        return _fake_l2_report()

    return fake_l2


async def _emit(*_a, **_k):
    return None


async def _health():
    return {}


# --------------------------------------------------------------------------- OFF: no run row
async def test_off_writes_no_analysis_run_row(metering_org, db_session):
    """entitlements_enabled=False ⇒ the analyze path writes NO analysis_run row
    (byte-identical). The cascade runs exactly as today."""
    org_id = metering_org
    await db_session.commit()
    pid = await _make_project(org_id)
    seen: dict[str, object] = {}

    async def fake_l2(project_id, *, max_concurrency, limit, force, as_of, analysis_run_id=None):
        seen["run_id"] = analysis_run_id  # OFF ⇒ must be None (no run row threaded)
        return _fake_l2_report()

    off = _settings(entitlements_enabled=False)
    with (
        patch.object(projects_api, "get_settings", lambda: off),
        patch.object(projects_api.metering, "get_settings", lambda: off),
        patch.object(projects_api, "run_l2", fake_l2),
        patch.object(projects_api, "emit", _emit),
        patch.object(projects_api.call_stats, "citation_health", _health),
    ):
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2"]))

    assert resp.layers == ["L2"]
    assert resp.notes == []
    assert seen["run_id"] is None  # OFF ⇒ no run_id threaded (byte-identical)
    n_runs = (
        await db_session.execute(
            select(func.count(AnalysisRun.id)).where(AnalysisRun.org_id == org_id)
        )
    ).scalar_one()
    assert n_runs == 0  # no run row at default flag


# --------------------------------------------------------------------------- ON: one run row + roll-up
async def test_on_writes_one_run_row_with_org_and_rolled_cost(metering_org, db_session):
    """entitlements_enabled=True ⇒ exactly one analysis_run row with org_id set,
    the tier snapshot, the tier answer_mode, status='done', and cost_usd =
    SUM(api_call cost)."""
    org_id = metering_org
    await subscribe(db_session, org_id, BUSINESS_PLAN)  # tier=business (L4, compliance)
    await db_session.commit()  # so the cascade's sibling sessions see it
    pid = await _make_project(org_id)

    on = _settings(entitlements_enabled=True)
    with (
        patch.object(projects_api, "get_settings", lambda: on),
        patch.object(projects_api.metering, "get_settings", lambda: on),
        patch.object(projects_api, "run_l2", _spy_l2_writes_apicall(org_id, 0.0123)),
        patch.object(projects_api, "emit", _emit),
        patch.object(projects_api.call_stats, "citation_health", _health),
    ):
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2"]))

    assert resp.layers == ["L2"]
    runs = (
        (await db_session.execute(select(AnalysisRun).where(AnalysisRun.org_id == org_id)))
        .scalars()
        .all()
    )
    assert len(runs) == 1
    run = runs[0]
    assert run.org_id == org_id  # NOT NULL — real principal org (b3-06a discipline)
    assert run.tier == "business"
    assert run.answer_mode == "compliance"
    assert run.status == "done"
    assert run.project_id == pid
    # Roll-up: cost = SUM(api_call.cost_usd WHERE analysis_run_id=run).
    assert float(run.cost_usd) == pytest.approx(0.0123, abs=1e-6)
    n_apicalls = (
        await db_session.execute(
            select(func.count(ApiCall.id)).where(ApiCall.analysis_run_id == run.id)
        )
    ).scalar_one()
    assert n_apicalls == 1


# --------------------------------------------------------------------------- AC-COST-MODEL: non-zero L4 cost
async def test_l4_run_rolls_non_zero_cost(metering_org, db_session):
    """AC-COST-MODEL: an L4 run carries a non-zero cost_usd rolled from its tagged
    api_call rows (the L2/L3 LLM spend; L4 itself is the rule stub)."""
    org_id = metering_org
    await subscribe(db_session, org_id, BUSINESS_PLAN)
    await db_session.commit()
    pid = await _make_project(org_id)

    async def fake_l4(project_id, *, force, analysis_run_id=None):
        assert analysis_run_id is not None
        return _fake_l4_report()

    on = _settings(entitlements_enabled=True)
    with (
        patch.object(projects_api, "get_settings", lambda: on),
        patch.object(projects_api.metering, "get_settings", lambda: on),
        patch.object(projects_api, "run_l2", _spy_l2_writes_apicall(org_id, 0.05)),
        patch.object(projects_api, "run_l4_gaps", fake_l4),
        patch.object(projects_api, "emit", _emit),
        patch.object(projects_api.call_stats, "citation_health", _health),
    ):
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2", "L4"]))

    assert resp.layers == ["L2", "L4"]
    run = (
        (await db_session.execute(select(AnalysisRun).where(AnalysisRun.org_id == org_id)))
        .scalars()
        .one()
    )
    assert "L4" in run.executed_layers
    assert float(run.cost_usd) > 0.0  # non-zero L4 run cost


# --------------------------------------------------------------------------- roll-up unit
async def test_finish_run_rolls_sum_of_tagged_apicalls(metering_org, db_session):
    """finish_analysis_run(status='done') rolls cost_usd = SUM(api_call.cost_usd)
    over the rows tagged with the run — proven directly."""
    from regsuper.entitlements import metering as met
    from regsuper.entitlements.models import EffectiveEntitlement

    org_id = metering_org
    ent = EffectiveEntitlement(org_id=org_id, plan_code="business", tier=2, max_depth="L4")
    run_id = await met.open_analysis_run(
        db_session,
        org_id=org_id,
        project_id=None,
        entitlement=ent,
        requested_layers=["L2", "L3"],
        executed_layers=["L2", "L3"],
    )
    assert run_id is not None
    for c in (0.001, 0.002, 0.004):
        db_session.add(
            ApiCall(
                provider="openai",
                model="m",
                endpoint="chat.completions",
                source="analysis.l3",
                org_id=org_id,
                analysis_run_id=run_id,
                tokens_input=1,
                tokens_output=1,
                tokens_cached=0,
                cost_usd=c,
                latency_ms=1,
                status="ok",
                request_json={},
                response_json={},
                response_summary="",
            )
        )
    await db_session.flush()
    await met.finish_analysis_run(db_session, run_id, status="done", executed_layers=["L2", "L3"])
    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "done"
    assert float(run.cost_usd) == pytest.approx(0.007, abs=1e-6)


# --------------------------------------------------------------------------- None run_id is a no-op
async def test_open_run_off_returns_none_no_write(metering_org, db_session):
    """open_analysis_run(entitlement=None) writes nothing and returns None — the
    default-OFF guarantee at the helper level."""
    from regsuper.entitlements import metering as met

    org_id = metering_org
    run_id = await met.open_analysis_run(
        db_session,
        org_id=org_id,
        project_id=None,
        entitlement=None,
        requested_layers=["L2"],
        executed_layers=["L2"],
    )
    assert run_id is None
    n = (
        await db_session.execute(
            select(func.count(AnalysisRun.id)).where(AnalysisRun.org_id == org_id)
        )
    ).scalar_one()
    assert n == 0
