"""C3 (T-C3a / AS-3 + AS-4) — the durable analysis_run lifecycle + idempotency.

DB-backed (the seeded Docker Postgres); SKIPs cleanly without it. Proves:
  * open_run writes a durable analysis_run row that survives the session (unlike the
    in-memory cascade_tracker ring) and carries the real org_id (NOT NULL, b3-06a);
  * the state machine: queued → running → done / failed / refused;
  * terminal states are idempotent (re-finishing a closed run is a no-op);
  * reserve_idempotent_run: two submits with the SAME (org_id, key) → ONE run row;
    different keys → two runs; no key → None (the legacy/sync passthrough).
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select

from regsuper.analysis import run_lifecycle as lc
from regsuper.analysis import tasks
from regsuper.storage.models import AnalysisRun


# ── AS-3: the durable state machine ──────────────────────────────────────────
async def test_open_run_is_durable_queued_then_running_then_done(metering_org, db_session):
    """A run opened queued, transitioned running → done, is durable and ends 'done'."""
    org_id = metering_org
    run_id = await lc.open_run(
        db_session,
        org_id=org_id,
        project_id=None,
        requested_layers=["L2", "L3"],
        status=lc.QUEUED,
    )
    await db_session.commit()  # durability: commit and re-read on a fresh get
    assert run_id is not None

    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "queued"
    assert run.org_id == org_id  # NOT NULL — real principal org
    assert run.started_at is None  # queued has not started
    assert run.requested_depth == "L3"  # deepest requested

    await lc.mark_running(db_session, run_id, worker_id="test-worker")
    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "running"
    assert run.started_at is not None
    assert run.worker_id == "test-worker"

    await lc.mark_done(db_session, run_id, executed_layers=["L2", "L3"])
    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "done"
    assert run.finished_at is not None
    assert run.executed_layers == ["L2", "L3"]


async def test_run_can_be_marked_failed(metering_org, db_session):
    org_id = metering_org
    run_id = await lc.open_run(
        db_session,
        org_id=org_id,
        project_id=None,
        requested_layers=["L2"],
        status=lc.RUNNING,
    )
    await lc.mark_failed(db_session, run_id, error="boom")
    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "failed"
    assert run.error == "boom"
    assert run.finished_at is not None


async def test_run_can_be_marked_refused(metering_org, db_session):
    org_id = metering_org
    run_id = await lc.open_run(
        db_session,
        org_id=org_id,
        project_id=None,
        requested_layers=["L2"],
        status=lc.RUNNING,
    )
    await lc.mark_refused(db_session, run_id, reason="reserved_advice")
    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "refused"
    assert run.error == "reserved_advice"


async def test_terminal_state_is_idempotent(metering_org, db_session):
    """Re-finishing an already-terminal run leaves the first terminal status."""
    org_id = metering_org
    run_id = await lc.open_run(
        db_session,
        org_id=org_id,
        project_id=None,
        requested_layers=["L2"],
        status=lc.RUNNING,
    )
    await lc.mark_done(db_session, run_id)
    # A late/duplicate failed callback must NOT overwrite 'done'.
    await lc.mark_failed(db_session, run_id, error="late")
    run = await db_session.get(AnalysisRun, run_id)
    assert run.status == "done"
    assert run.error != "late"


async def test_lifecycle_helpers_noop_on_none_run_id(metering_org, db_session):
    """None run_id (lifecycle inert / default-OFF) → every helper is a no-op."""
    org_id = metering_org
    await lc.mark_running(db_session, None)
    await lc.mark_done(db_session, None)
    await lc.mark_failed(db_session, None, error="x")
    await lc.mark_refused(db_session, None, reason="y")
    n = (
        await db_session.execute(
            select(func.count(AnalysisRun.id)).where(AnalysisRun.org_id == org_id)
        )
    ).scalar_one()
    assert n == 0  # nothing written


# ── AS-4: idempotency ─────────────────────────────────────────────────────────
async def test_no_key_returns_none_passthrough(metering_org, db_session):
    """No idempotency key → None (the legacy/sync path; byte-identical)."""
    org_id = metering_org
    out = await tasks.reserve_idempotent_run(
        db_session,
        org_id=org_id,
        idempotency_key=None,
        project_id=None,
        requested_layers=["L2"],
    )
    assert out is None
    n = (
        await db_session.execute(
            select(func.count(AnalysisRun.id)).where(AnalysisRun.org_id == org_id)
        )
    ).scalar_one()
    assert n == 0


async def test_same_key_one_run(metering_org, db_session):
    """Two submits with the SAME (org_id, key) → ONE run row; the second sees
    created=False (so the caller skips the cascade + the bill)."""
    org_id = metering_org
    key = f"idem-{uuid4().hex}"
    first = await tasks.reserve_idempotent_run(
        db_session,
        org_id=org_id,
        idempotency_key=key,
        project_id=None,
        requested_layers=["L2", "L3"],
    )
    assert first is not None and first.created is True
    second = await tasks.reserve_idempotent_run(
        db_session,
        org_id=org_id,
        idempotency_key=key,
        project_id=None,
        requested_layers=["L2", "L3"],
    )
    assert second is not None and second.created is False
    assert second.run_id == first.run_id  # SAME run
    n = (
        await db_session.execute(
            select(func.count(AnalysisRun.id)).where(
                AnalysisRun.org_id == org_id, AnalysisRun.idempotency_key == key
            )
        )
    ).scalar_one()
    assert n == 1  # exactly one run for the key


async def test_different_keys_two_runs(metering_org, db_session):
    org_id = metering_org
    a = await tasks.reserve_idempotent_run(
        db_session,
        org_id=org_id,
        idempotency_key=f"k-{uuid4().hex}",
        project_id=None,
        requested_layers=["L2"],
    )
    b = await tasks.reserve_idempotent_run(
        db_session,
        org_id=org_id,
        idempotency_key=f"k-{uuid4().hex}",
        project_id=None,
        requested_layers=["L2"],
    )
    assert a is not None and b is not None
    assert a.run_id != b.run_id  # two distinct runs
    n = (
        await db_session.execute(
            select(func.count(AnalysisRun.id)).where(AnalysisRun.org_id == org_id)
        )
    ).scalar_one()
    assert n == 2
