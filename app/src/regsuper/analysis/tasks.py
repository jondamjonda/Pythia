"""The analysis cascade body, its Celery task wrapper and the idempotency guard.

``run_analysis_job(job)`` is the single place the L2, L3 and L4 cascade runs. Both entry paths
call it the same way:

* the synchronous path (``analyze_async`` off, the default) awaits it directly and returns the
  ``AnalyzeResponse`` (covered by a snapshot test);
* the Celery task path (``run_analysis_task`` below) calls the same body. In eager mode
  (``task_always_eager=True``, the default) it runs inline with no broker.

The body runs the already-clamped layers, builds the L2/L3/L4 sections, records failures and
refunds metered usage on error, and on success emits completion events, closes the metered run,
records usage and returns the ``AnalyzeResponse``. The gates that come before the cascade (the
resource refuse-to-start gate, the compliance input gate that refuses reserved-advice requests, the
tier clamp, opening the run row and the reserve that returns 429 before any spend) stay in the
endpoint, which passes the result in as a fully resolved ``CascadeJob``. When the input gate
refuses in enforce mode, the endpoint returns before calling the body.

The cascade runners (``run_l2``, ``run_l3``, ``run_l4_gaps``), ``emit``, ``get_settings``,
``call_stats``, ``cascade_tracker`` and ``metering`` are looked up through the
``regsuper.api.projects`` module at call time, so tests that
``patch.object(projects_api, "run_l2", ...)`` intercept the cascade.

``reserve_idempotent_run(...)`` claims a durable ``analysis_run`` row keyed by
``(org_id, idempotency_key)`` (a unique index from migration 019). Two submissions with the same
key get the same run row (one run, one bill); different keys get two runs. Without a key the guard
does nothing. Only the task path consults it.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from regsuper.analysis.celery_app import app

if TYPE_CHECKING:  # avoid import cycles and heavy imports at module load
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from regsuper.api.projects import AnalyzeResponse


@dataclass
class CascadeJob:
    """The fully resolved inputs the cascade body needs.

    Everything the endpoint decided before the cascade: gates, tier clamp and metering
    reservation. With entitlements off the metering fields stay None or empty.
    """

    project_id: UUID
    layers: list[str]  # the layers to execute (clamped when entitlements are on)
    as_of: date
    max_concurrency: int = 8
    limit: int | None = None
    force: bool = False
    # Control-room tracker handle (the cascade_tracker.start_run dict); opaque to the body.
    tracker_run: dict[str, Any] | None = None
    law_source: str = "regsuper"
    # Metering handles; None unless entitlements are on (then no run row is written).
    met_run_id: int | None = None
    met_org_id: UUID | None = None
    met_reserved: list[str] = field(default_factory=list)
    met_note: str | None = None


async def run_analysis_job(job: CascadeJob) -> AnalyzeResponse | Any:
    """Execute the cascade for ``job`` and return the ``AnalyzeResponse``.

    Shared by the synchronous path and the Celery task. On error it records the failure,
    refunds metered usage and re-raises; on success it emits completion events, closes the
    metered run and records usage.
    """
    # Resolve collaborators through the api.projects module so test patches on
    # projects_api.run_l2, run_l3, run_l4_gaps, emit, call_stats and get_settings
    # still intercept them.
    from regsuper.analysis import LawSourceNotSupported
    from regsuper.api import projects as projects_api
    from regsuper.api.projects import (
        AnalyzeResponse,
        L2Section,
        L3Section,
        L4Section,
        _meter_on_failure,
        _run_cost_usd,
    )
    from regsuper.storage.database import session_scope

    t0 = _time.perf_counter()
    l2_out: L2Section | None = None
    l3_out: L3Section | None = None
    l4_out: L4Section | None = None
    _layers = job.layers
    run = job.tracker_run

    try:
        if "L2" in _layers:
            rep2 = await projects_api.run_l2(
                job.project_id,
                max_concurrency=job.max_concurrency,
                limit=job.limit,
                force=job.force,
                as_of=job.as_of,
                analysis_run_id=job.met_run_id,
            )
            l2_out = L2Section(
                n_candidates=rep2.n_candidates,
                n_evaluated=rep2.n_evaluated,
                n_skipped_cached=rep2.n_skipped_cached,
                n_applicable=rep2.n_applicable,
                n_ambiguous=rep2.n_ambiguous,
                n_not_applicable=rep2.n_not_applicable,
                n_refused=rep2.n_refused,
                n_errors=rep2.n_errors,
                n_findings_persisted=len(rep2.findings),
                sample_errors=rep2.sample_errors,
                latency_ms=rep2.total_latency_ms,
            )
        if "L3" in _layers:
            rep3 = await projects_api.run_l3(
                job.project_id,
                max_concurrency=job.max_concurrency,
                force=job.force,
                analysis_run_id=job.met_run_id,
            )
            l3_out = L3Section(
                n_provisions=rep3.n_provisions,
                n_evaluated=rep3.n_evaluated,
                n_skipped_cached=rep3.n_skipped_cached,
                n_obligations_extracted=rep3.n_obligations_extracted,
                n_obligations_persisted=rep3.n_obligations_persisted,
                n_errors=rep3.n_errors,
                sample_errors=rep3.sample_errors,
                latency_ms=rep3.total_latency_ms,
            )
            # Applicability scoring between L3 and L4, only with entity_scoping_enabled on.
            if projects_api.get_settings().entity_scoping_enabled:
                await projects_api.run_applicability_scoring(job.project_id, force=job.force)
        if "L4" in _layers:
            rep4 = await projects_api.run_l4_gaps(
                job.project_id,
                force=job.force,
                analysis_run_id=job.met_run_id,
            )
            l4_out = L4Section(
                n_obligations_scanned=rep4.n_obligations_scanned,
                n_obligations_with_evidence=rep4.n_obligations_with_evidence,
                n_gaps_persisted=rep4.n_gaps_persisted,
                n_evidence_covered=rep4.n_evidence_covered,
                n_client_documents=rep4.n_client_documents,
                sample_errors=rep4.sample_errors,
                latency_ms=rep4.total_latency_ms,
            )
    except LookupError as exc:
        projects_api.cascade_tracker.fail_run(run, None, str(exc))
        await _meter_on_failure(
            job.met_run_id, job.met_org_id, job.met_reserved, status="failed", error=str(exc)
        )
        await projects_api.emit(
            "analysis.run.failed",
            module="polarius",
            level="error",
            pid=str(job.project_id),
            reason=str(exc),
        )
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LawSourceNotSupported as exc:
        # The law-source gate refused this law_source: answer 409 (a clear config
        # conflict) rather than return a silently empty analysis.
        projects_api.cascade_tracker.fail_run(run, "law_source_gate", str(exc))
        await _meter_on_failure(
            job.met_run_id, job.met_org_id, job.met_reserved, status="refused", error=str(exc)
        )
        await projects_api.emit(
            "analysis.law_source.refused",
            module="polarius",
            level="warn",
            pid=str(job.project_id),
            law_source=job.law_source,
            reason=str(exc),
        )
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        projects_api.cascade_tracker.fail_run(run, "unknown", repr(exc))
        await _meter_on_failure(
            job.met_run_id, job.met_org_id, job.met_reserved, status="failed", error=repr(exc)
        )
        await projects_api.emit(
            "analysis.run.failed",
            module="polarius",
            level="error",
            pid=str(job.project_id),
            reason=repr(exc)[:200],
        )
        raise

    counts = {
        "applicable": l2_out.n_applicable if l2_out else None,
        "obligations": l3_out.n_obligations_persisted if l3_out else None,
        "gaps": l4_out.n_gaps_persisted if l4_out else None,
    }
    projects_api.cascade_tracker.finish_run(run, counts)
    health = await projects_api.call_stats.citation_health()
    await projects_api.emit(
        "analysis.run.complete",
        module="polarius",
        pid=str(job.project_id),
        applicable=counts["applicable"],
        obligations=counts["obligations"],
        gaps=counts["gaps"],
        yield_pct=health.get("cascade_grounded_yield_pct"),
    )
    await projects_api.emit(
        "analysis.citations.summary",
        module="polarius",
        grounded=health.get("grounded"),
        total=health.get("total"),
        unverified_rate=health.get("unverified_citation_rate_pct"),
    )

    # Close the metered run (status 'done', roll up cost_usd) and record usage. Skipped
    # when met_run_id is None (entitlements off, so there is no run row).
    _notes: list[str] = [job.met_note] if job.met_note else []
    if job.met_run_id is not None and job.met_org_id is not None:
        async with session_scope() as _fsession:
            await projects_api.metering.finish_analysis_run(
                _fsession,
                job.met_run_id,
                status="done",
                executed_layers=_layers,
            )
            _run_cost = await _run_cost_usd(_fsession, job.met_run_id)
            await projects_api.metering.record_run_usage(
                _fsession,
                org_id=job.met_org_id,
                reserved_metrics=job.met_reserved,
                cost_usd=_run_cost,
            )

    total_ms = int((_time.perf_counter() - t0) * 1000)
    return AnalyzeResponse(
        project_id=job.project_id,
        layers=_layers,
        l2=l2_out,
        l3=l3_out,
        l4=l4_out,
        total_latency_ms=total_ms,
        notes=_notes,
    )


# ── Idempotency guard ───────────────────────────────────────────────────────────
@dataclass
class IdempotentRun:
    """The outcome of an idempotency claim.

    Holds the durable run id and whether this submission created it. ``created=False`` means
    an earlier submission with the same key owns the run, so the caller must not re-run the
    cascade or bill again.
    """

    run_id: int
    created: bool


async def reserve_idempotent_run(
    session: AsyncSession,
    *,
    org_id: UUID,
    idempotency_key: str | None,
    project_id: UUID | None,
    requested_layers: list[str],
    executed_layers: list[str] | None = None,
    tier: str | None = None,
    answer_mode: str = "information",
) -> IdempotentRun | None:
    """Claim, or look up, the durable run for ``(org_id, idempotency_key)``.

    * No key: returns None (no idempotency requested; the synchronous path passes none).
    * First submission for the key: inserts a queued ``analysis_run`` row and returns
      ``IdempotentRun(run_id, created=True)``.
    * A repeat submission: returns the existing row as ``IdempotentRun(run_id, created=False)``
      so the caller skips the cascade.

    The unique ``(org_id, idempotency_key)`` index (migration 019) is the source of truth: a
    concurrent INSERT that loses the race raises ``IntegrityError`` and the winner's row is
    re-selected, so the guarantee holds under parallel submissions. ``org_id`` is required
    (the column is NOT NULL).
    """
    if not idempotency_key:
        return None
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from regsuper.analysis import run_lifecycle
    from regsuper.storage.models import AnalysisRun

    existing = (
        (
            await session.execute(
                select(AnalysisRun).where(
                    AnalysisRun.org_id == org_id,
                    AnalysisRun.idempotency_key == idempotency_key,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return IdempotentRun(run_id=existing.id, created=False)

    try:
        run_id = await run_lifecycle.open_run(
            session,
            org_id=org_id,
            project_id=project_id,
            requested_layers=requested_layers,
            executed_layers=executed_layers,
            status=run_lifecycle.QUEUED,
            tier=tier,
            answer_mode=answer_mode,
            idempotency_key=idempotency_key,
        )
        await session.flush()
        return IdempotentRun(run_id=run_id, created=True)
    except IntegrityError:
        # A concurrent submission won the unique (org_id, idempotency_key); re-select it.
        await session.rollback()
        winner = (
            (
                await session.execute(
                    select(AnalysisRun).where(
                        AnalysisRun.org_id == org_id,
                        AnalysisRun.idempotency_key == idempotency_key,
                    )
                )
            )
            .scalars()
            .first()
        )
        if winner is None:  # pragma: no cover — the conflict implies a row exists
            raise
        return IdempotentRun(run_id=winner.id, created=False)


# ── Celery task wrapper ─────────────────────────────────────────────────────────
@app.task(name="regsuper.analysis.run_analysis_task")
def run_analysis_task(job: CascadeJob) -> AnalyzeResponse | Any:
    """Run ``run_analysis_job`` as a Celery task.

    In eager mode (the default) this runs inline in the calling process with no broker. It
    bridges Celery's synchronous task contract to the async cascade body by running it on a
    fresh event loop. With a real broker, the same body executes on a worker.

    A process that already has a running event loop must await ``run_analysis_job`` directly
    instead, which is what the synchronous endpoint does. This wrapper is the entry point for
    the task path and for broker-backed workers.
    """
    import asyncio

    return asyncio.run(run_analysis_job(job))


__all__ = [
    "CascadeJob",
    "IdempotentRun",
    "reserve_idempotent_run",
    "run_analysis_job",
    "run_analysis_task",
]
