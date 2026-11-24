"""Durable lifecycle records for analysis runs, stored in the ``analysis_run`` table.

``observability/cascade_tracker.py`` keeps an in-memory ring of the last ~50 runs for the control
room's cascade board, but that ring is process-local: it does not survive a restart, cannot be
shared with a worker process and cannot back idempotency or resume. This module persists the run
lifecycle to ``analysis_run`` (BIGINT id, migration 011), which the API and a Celery worker can
both read and write.

State machine::

    queued --> running --> done
                  |------> failed
                  `------> refused

* ``queued``: the run is accepted (for example enqueued for a worker) but the cascade has not
  started. Only the task path opens a run in this state; the synchronous path opens directly in
  ``running`` via the metering helper.
* ``running``: the cascade is executing.
* ``done``: every requested (or clamped) layer completed.
* ``failed``: an unexpected error aborted the cascade.
* ``refused``: a gate declined (reserved advice, quota, law source). Kept distinct from
  ``failed`` so an honest refusal is auditable as such.

Nothing here writes a row on its own. The synchronous analyze path (``analyze_async`` and
``entitlements_enabled`` both off) writes no ``analysis_run`` row at all. Rows are created only
when the task path opens one (even the eager default writes a queued or running row, so the
lifecycle is testable) or when the metering helpers open one under ``entitlements_enabled``.

Finishing an already terminal run is a no-op, so a retry or double callback cannot corrupt the
record. A CHECK constraint from migration 011 enforces the status vocabulary in the database.

``analysis_run.org_id`` is NOT NULL, so ``open_run`` requires a real ``org_id`` (the sentinel org
when running locally).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from regsuper.storage.models import AnalysisRun

if TYPE_CHECKING:  # keep the async-session import out of module load
    from sqlalchemy.ext.asyncio import AsyncSession

# The allowed lifecycle vocabulary (mirrors the migration 011 CHECK on analysis_run.status).
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
REFUSED = "refused"

_TERMINAL: frozenset[str] = frozenset({DONE, FAILED, REFUSED})
_VALID: frozenset[str] = frozenset({QUEUED, RUNNING, DONE, FAILED, REFUSED})


async def open_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    project_id: UUID | None,
    requested_layers: list[str],
    executed_layers: list[str] | None = None,
    status: str = QUEUED,
    tier: str | None = None,
    answer_mode: str = "information",
    requested_depth: str | None = None,
    idempotency_key: str | None = None,
    as_of: date | None = None,
    worker_id: str | None = None,
) -> int:
    """Insert one durable ``analysis_run`` row and return its id.

    Opens in ``queued`` by default (the task entry point); pass ``status='running'`` for a
    synchronous run that starts immediately. ``org_id`` is required (the column is NOT NULL).
    Works within the caller's session and only flushes; the row is durable once the caller
    commits.
    """
    if status not in _VALID:
        raise ValueError(f"invalid run status: {status!r}")
    started = datetime.now(UTC) if status == RUNNING else None
    run = AnalysisRun(
        org_id=org_id,
        project_id=project_id,
        tier=tier,
        requested_depth=requested_depth or _highest_depth(requested_layers),
        requested_layers=list(requested_layers),
        executed_layers=list(executed_layers if executed_layers is not None else []),
        status=status,
        answer_mode=answer_mode,
        idempotency_key=idempotency_key,
        as_of=as_of,
        worker_id=worker_id,
        started_at=started,
    )
    session.add(run)
    await session.flush()  # populate run.id
    return run.id


async def mark_running(
    session: AsyncSession,
    run_id: int | None,
    *,
    worker_id: str | None = None,
) -> None:
    """Move a run from queued to running, stamping ``started_at`` once.

    No-op when ``run_id`` is None (no lifecycle tracking) or the run is already terminal.
    """
    if run_id is None:
        return
    run = await session.get(AnalysisRun, run_id)
    if run is None or run.status in _TERMINAL:
        return
    run.status = RUNNING
    if run.started_at is None:
        run.started_at = datetime.now(UTC)
    if worker_id is not None:
        run.worker_id = worker_id
    await session.flush()


async def mark_done(
    session: AsyncSession,
    run_id: int | None,
    *,
    executed_layers: list[str] | None = None,
) -> None:
    """Mark a run done and stamp ``finished_at``.

    The per-run cost roll-up (``cost_usd = SUM(api_call.cost_usd)``) belongs to the metering
    helper on the metered path and is not duplicated here. No-op when ``run_id`` is None or the
    run is already terminal.
    """
    await _finish(session, run_id, status=DONE, executed_layers=executed_layers)


async def mark_failed(
    session: AsyncSession,
    run_id: int | None,
    *,
    error: str | None = None,
    executed_layers: list[str] | None = None,
) -> None:
    """Mark a run failed after an unexpected error.

    No-op when ``run_id`` is None or the run is already terminal.
    """
    await _finish(session, run_id, status=FAILED, error=error, executed_layers=executed_layers)


async def mark_refused(
    session: AsyncSession,
    run_id: int | None,
    *,
    reason: str | None = None,
    executed_layers: list[str] | None = None,
) -> None:
    """Mark a run refused because a gate declined it (reserved advice, quota, law source).

    No-op when ``run_id`` is None or the run is already terminal.
    """
    await _finish(session, run_id, status=REFUSED, error=reason, executed_layers=executed_layers)


async def _finish(
    session: AsyncSession,
    run_id: int | None,
    *,
    status: str,
    error: str | None = None,
    executed_layers: list[str] | None = None,
) -> None:
    if run_id is None:
        return
    run = await session.get(AnalysisRun, run_id)
    if run is None or run.status in _TERMINAL:
        # Idempotent: a run already closed (by a retry or double callback, say) is
        # left untouched, so the first terminal status stands.
        return
    run.status = status
    run.finished_at = datetime.now(UTC)
    if executed_layers is not None:
        run.executed_layers = list(executed_layers)
    if error is not None:
        run.error = error[:2000]
    await session.flush()


def _highest_depth(layers: list[str]) -> str | None:
    """Return the deepest requested layer (L4, then L3, then L2) for requested_depth.

    The migration 011 CHECK accepts only L2, L3 and L4.
    """
    rank = {"L2": 2, "L3": 3, "L4": 4}
    best: tuple[int, str] | None = None
    for layer in layers:
        r = rank.get(layer)
        if r is not None and (best is None or r > best[0]):
            best = (r, layer)
    return best[1] if best else None


__all__ = [
    "DONE",
    "FAILED",
    "QUEUED",
    "REFUSED",
    "RUNNING",
    "mark_done",
    "mark_failed",
    "mark_refused",
    "mark_running",
    "open_run",
]
