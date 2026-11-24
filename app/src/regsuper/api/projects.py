"""Project endpoints: intake, candidate retrieval and the analysis cascade.

Create and fetch projects, list L1 candidate provisions (metadata only), run the L2 (applicability),
L3 (obligations) and L4 (gaps) cascade, and list the persisted findings.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

# The cascade itself runs in analysis/tasks.py, but these names stay imported here on purpose:
# tasks.run_analysis_job resolves them through this module (projects_api.run_l2, run_l3,
# run_l4_gaps, run_applicability_scoring, call_stats), and many tests patch them on this module
# with patch.object(projects_api, "run_l2", ...). A linter sees them as unused, hence the noqa,
# but removing them would break both the cascade and the tests.
from regsuper.analysis.applicability_scoring import run_applicability_scoring  # noqa: F401
from regsuper.analysis.compliance_guard import classify_answer_mode, disclaimer_for
from regsuper.analysis.l0_intake import ProjectIntake, create_project, get_project
from regsuper.analysis.l1_candidates import l1_candidates
from regsuper.analysis.l2_applicability import run_l2  # noqa: F401
from regsuper.analysis.l3_obligations import run_l3  # noqa: F401
from regsuper.analysis.l4_gaps import run_l4_gaps  # noqa: F401
from regsuper.analysis.tasks import CascadeJob, run_analysis_job
from regsuper.api.auth import get_principal
from regsuper.config import get_settings
from regsuper.entitlements import metering
from regsuper.observability import (
    call_stats,  # noqa: F401  (patched by tests; see above)
    cascade_tracker,
    resource_guard,
)
from regsuper.observability.events import emit
from regsuper.observability.limits import QuotaExceeded
from regsuper.storage.database import session_scope
from regsuper.storage.models import Finding

router = APIRouter()


class ProjectCreateResponse(BaseModel):
    id: UUID
    name: str
    sector: str
    jurisdiction_scope: list[str]
    entity_type: str | None
    sector_tags: list[str]
    activity_tags: list[str]
    metadata: dict[str, Any]


@router.post("/api/projects", response_model=ProjectCreateResponse)
async def post_project(intake: ProjectIntake, request: Request) -> ProjectCreateResponse:
    """Create a project from the intake form and return its derived tags.

    The derived tags are what the analysis cascade uses downstream. The project belongs to the
    caller's organization. Returns 401 without a valid session when authentication is required.
    """
    # The owning org comes from the request principal. With require_auth=False (local dev) that is
    # the anonymous sentinel org, the same org migration 007 attached to pre-tenancy rows.
    p = await get_principal(request)
    project = await create_project(intake, client_id=p.org_id)
    meta = project.metadata_ or {}
    return ProjectCreateResponse(
        id=project.id,
        name=project.name,
        sector=project.sector,
        jurisdiction_scope=list(project.jurisdiction_scope),
        entity_type=project.entity_type,
        sector_tags=list(meta.get("sector_tags") or []),
        activity_tags=list(meta.get("activity_tags") or []),
        metadata=meta,
    )


@router.get("/api/projects/{project_id}", response_model=ProjectCreateResponse)
async def get_project_route(project_id: UUID) -> ProjectCreateResponse:
    """Return one project with its derived tags. Returns 404 if it does not exist."""
    project = await get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    meta = project.metadata_ or {}
    return ProjectCreateResponse(
        id=project.id,
        name=project.name,
        sector=project.sector,
        jurisdiction_scope=list(project.jurisdiction_scope),
        entity_type=project.entity_type,
        sector_tags=list(meta.get("sector_tags") or []),
        activity_tags=list(meta.get("activity_tags") or []),
        metadata=meta,
    )


class CandidateOut(BaseModel):
    provision_id: UUID
    label: str | None
    legal_hierarchy: str
    jurisdiction: str
    source_external_id: str | None
    source_title: str | None
    priority_weight: int


class CandidatesResponse(BaseModel):
    project_id: UUID
    n_candidates: int
    candidates: list[CandidateOut]


@router.get("/api/projects/{project_id}/candidates", response_model=CandidatesResponse)
async def project_candidates(
    project_id: UUID,
    top_k: int = 200,
) -> CandidatesResponse:
    """Return up to `top_k` candidate provisions from the L1 metadata filter.

    A wide-net filter on metadata only: cheap, idempotent and without any LLM call.
    """
    try:
        rows = await l1_candidates(project_id, top_k=top_k)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CandidatesResponse(
        project_id=project_id,
        n_candidates=len(rows),
        candidates=[
            CandidateOut(
                provision_id=r.provision_id,
                label=r.label,
                legal_hierarchy=r.legal_hierarchy,
                jurisdiction=r.jurisdiction,
                source_external_id=r.source_external_id,
                source_title=r.source_title,
                priority_weight=r.priority_weight,
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------------------
# Analysis cascade (L2 applicability, L3 obligations, L4 gaps)
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    top_k: int = Field(default=200, ge=1, le=1000)
    limit: int | None = Field(default=None, ge=1, le=1000)
    max_concurrency: int = Field(default=8, ge=1, le=32)
    force: bool = False
    # Point in time: analyse the law as it stood on this date (default: today). Applied to the L1
    # retrieval window.
    as_of: date | None = Field(default=None)
    layers: list[Literal["L2", "L3", "L4"]] = Field(
        default_factory=lambda: ["L2", "L3", "L4"],
        description=(
            "Which analysis layers to run. Order is fixed (L2 → L3 → L4); "
            "passing a subset just skips the others."
        ),
    )


class L2Section(BaseModel):
    n_candidates: int
    n_evaluated: int
    n_skipped_cached: int
    n_applicable: int
    n_ambiguous: int
    n_not_applicable: int
    n_refused: int = 0  # applicable verdicts below the confidence floor, recorded as cited refusals
    n_errors: int
    n_findings_persisted: int
    sample_errors: list[str]
    latency_ms: int


class L3Section(BaseModel):
    n_provisions: int
    n_evaluated: int
    n_skipped_cached: int
    n_obligations_extracted: int
    n_obligations_persisted: int
    n_errors: int
    sample_errors: list[str]
    latency_ms: int


class L4Section(BaseModel):
    n_obligations_scanned: int
    n_obligations_with_evidence: int
    n_gaps_persisted: int
    n_evidence_covered: int
    n_client_documents: int
    sample_errors: list[str]
    latency_ms: int


class AnalyzeResponse(BaseModel):
    project_id: UUID
    layers: list[str]
    l2: L2Section | None = None
    l3: L3Section | None = None
    l4: L4Section | None = None
    total_latency_ms: int
    # Notes about the request, empty by default. When entitlements limit the analysis depth (for
    # example a Lite plan asking for L4 runs only L2), a `layers_capped: ...` note is added and the
    # request still returns 200, never 403. `layers` lists the layers that actually ran.
    notes: list[str] = Field(default_factory=list)


class ComplianceRefusalResponse(BaseModel):
    """Refusal returned (with HTTP 200) when the input gate classifies a request as reserved advice.

    No cascade runs and no finding is written; a lawyer hand-off is offered instead. Only returned
    when the gate enforces; in shadow mode (the default) the analysis proceeds normally.
    """

    project_id: UUID
    answer_mode: Literal["reserved_advice"] = "reserved_advice"
    refused: bool = True
    reason: str
    disclaimer_id: str
    handoff_id: str | None = None
    message: str = (
        "Tento dotaz vyžaduje právní radu, kterou smí poskytnout pouze advokát. "
        "Můžeme vás propojit s partnerským advokátem."
    )


async def _run_cost_usd(session, run_id: int) -> float:
    """Return the run's actual spend: the sum of ``api_call.cost_usd`` over rows tagged ``run_id``.

    Used for record_usage; mirrors the roll-up that finish_analysis_run computes.
    """
    from sqlalchemy import text as _sql_text

    cost = (
        await session.execute(
            _sql_text(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM api_call WHERE analysis_run_id = :rid"
            ),
            {"rid": run_id},
        )
    ).scalar_one()
    return float(cost or 0.0)


async def _meter_on_failure(
    run_id: int | None,
    org_id: UUID | None,
    reserved_metrics: list[str],
    *,
    status: str,
    error: str,
) -> None:
    """Clean up metering after a refused or failed cascade.

    Refunds the reserved quota (a refusal must not consume the user's quota) and closes the run
    row with the failure status, in its own committed session. Does nothing when ``run_id`` is
    None (entitlements off). Swallows its own errors, so a cleanup failure never masks the
    cascade error the caller is re-raising.
    """
    if run_id is None:
        return
    try:
        async with session_scope() as session:
            if org_id is not None and reserved_metrics:
                await metering.refund_for_run(
                    session, org_id=org_id, reserved_metrics=reserved_metrics
                )
            await metering.finish_analysis_run(session, run_id, status=status, error=error)
    except Exception:
        pass


@router.post("/api/projects/{project_id}/analyze", response_model=AnalyzeResponse)
async def project_analyze(
    project_id: UUID,
    req: AnalyzeRequest | None = None,
) -> AnalyzeResponse | JSONResponse:
    """Run the analysis cascade (L2 applicability, L3 obligations, L4 gaps) for a project.

    Each layer is idempotent: re-runs skip provisions and obligations already evaluated. Pass
    `force=true` to bypass that cache, or e.g. `layers=["L2"]` to run a subset. `as_of` analyses
    the law as it stood on a given date (default: today).

    The call blocks until the requested layers complete. Returns 404 for an unknown project, 503
    when the host is over a resource ceiling, and 429 when the organization's quota is exhausted.
    When the input compliance gate enforces and the request needs reserved legal advice, it
    returns a `ComplianceRefusalResponse` (200) with a lawyer hand-off instead of an analysis.
    """
    req = req or AnalyzeRequest()
    as_of = req.as_of or date.today()  # point in time; defaults to today

    # Track the run for the control room's CascadeBoard and emit run-lifecycle events on the
    # __system__ channel (no project_id, so they go to the control room, not the project log).
    law_source = get_settings().law_source
    run = cascade_tracker.start_run(project_id, req.layers, law_source=law_source, force=req.force)
    await emit(
        "analysis.run.started",
        module="polarius",
        pid=str(project_id),
        layers=",".join(req.layers),
        law_source=law_source,
    )

    # Refuse to start any heavy LLM work if the host is already over a hard ceiling (RAM or CPU over
    # the refusal threshold, or a running GNN job over the MPS cap). macOS has no cgroups, so this
    # keeps the cascade from overloading the machine. The storage gate runs on the ingestion write
    # path instead; the cascade is read- and LLM-heavy, not disk-heavy.
    try:
        await resource_guard.gate_cpu_mem()
        await resource_guard.gate_mps()
    except resource_guard.ResourceLimitExceeded as exc:
        cascade_tracker.fail_run(run, "resource_cap", exc.reason)
        await emit(
            "analysis.run.refused",
            module="polarius",
            level="warn",
            pid=str(project_id),
            kind=exc.kind,
            reason=exc.reason,
        )
        raise HTTPException(status_code=503, detail=exc.reason) from exc

    # Input compliance gate: classify the project's declared intent before the cascade and before
    # any LLM spend. In shadow mode (the default) it writes one compliance.answer_mode.judged audit
    # row but does not block. In enforce mode (reserved for after legal sign-off) a reserved-advice
    # intent short-circuits to a 200 ComplianceRefusalResponse with a lawyer hand-off: no cascade,
    # no finding. The gate lives only on this product entry point, never on /internal/retrieve.
    # Setting compliance_guard_enabled=False disables it entirely.
    settings = get_settings()
    if getattr(settings, "compliance_guard_enabled", False):
        _proj = await get_project(project_id)
        _intent = (
            (getattr(_proj, "declared_intent", None) or "") if _proj is not None else ""
        ).strip()
        if _intent:
            # Pass the project's org so the compliance.answer_mode.judged audit row is accepted
            # under enforced row-level security, which rejects inserts with a NULL org.
            _verdict = await classify_answer_mode(
                text=_intent,
                project_id=project_id,
                where="input",
                org_id=getattr(_proj, "org_id", None),
            )
            if (
                getattr(settings, "compliance_guard_mode", "shadow") == "enforce"
                and _verdict.answer_mode == "reserved_advice"
            ):
                from regsuper.api.handoff import (
                    create_handoff_offer,  # imported lazily to avoid an import cycle
                )

                async with session_scope() as _session:
                    _offer = await create_handoff_offer(
                        _session,
                        trigger="reserved_advice",
                        reason=_verdict.reason,
                        answer_mode=_verdict.answer_mode,
                        project_id=project_id,
                        # Stamp the project's org so the hand-off row and its handoff.created
                        # audit event are accepted under enforced row-level security (a NULL-org
                        # insert as regsuper_app is rejected). _proj is not None here, because
                        # this branch only runs for a non-empty declared intent; getattr is
                        # merely defensive.
                        org_id=getattr(_proj, "org_id", None),
                        source_query=_intent[:500],
                        cited_provisions=[],
                    )
                    _handoff_id = str(_offer.id)
                cascade_tracker.fail_run(run, "reserved_advice", _verdict.reason)
                await emit(
                    "analysis.run.refused",
                    module="polarius",
                    level="warn",
                    pid=str(project_id),
                    reason="reserved_advice",
                )
                return JSONResponse(
                    status_code=200,
                    content=ComplianceRefusalResponse(
                        project_id=project_id,
                        reason=_verdict.reason,
                        disclaimer_id=disclaimer_for("reserved_advice"),
                        handoff_id=_handoff_id,
                    ).model_dump(mode="json"),
                )

    # ── Entitlements: plan-tier depth limit, metering and quota ───────────────────────────────────
    # Skipped entirely unless entitlements_enabled is set (off by default): layers are not limited,
    # no analysis_run row is written and no quota is reserved.
    #
    # When enabled: resolve the org's plan tier (failing closed), limit the requested layers to the
    # tier's depth with a note (a Lite plan asking for L4 runs L2 and still gets 200, never 403),
    # open the analysis_run row (status 'running', org_id set), then reserve quota. The reservation
    # raises 429 before any cascade work, so an over-quota request writes no api_call rows. The
    # reservation and the run row are committed in their own session, so the cascade's api_call
    # rows (written in separate sessions) can reference run_id, and the reservation holds under
    # concurrent requests.
    _layers = req.layers  # the layers that will actually run (limited when entitlements are on)
    _met_note: str | None = None
    _met_run_id: int | None = None
    _met_org_id: UUID | None = None
    _met_reserved: list[str] = []
    _ent = None
    if metering.entitlements_on():
        _met_project = await get_project(project_id)
        _met_org_id = getattr(_met_project, "org_id", None) if _met_project is not None else None
        if _met_org_id is None:
            # No org to meter against: fail closed rather than write a NULL-org row.
            cascade_tracker.fail_run(run, "entitlement", "no org_id on project")
            raise HTTPException(status_code=403, detail={"code": "entitlement.no_org"})
        _ent = await metering.resolve_for_entry(_met_org_id)
    if _ent is not None:
        _layers, _met_note = metering.clamp_layers(req.layers, _ent)
        async with session_scope() as _msession:
            _met_run_id = await metering.open_analysis_run(
                _msession,
                org_id=_met_org_id,
                project_id=project_id,
                entitlement=_ent,
                requested_layers=req.layers,
                executed_layers=_layers,
            )
            try:
                _met_reserved = await metering.reserve_for_run(
                    _msession,
                    org_id=_met_org_id,
                    entitlement=_ent,
                    executed_layers=_layers,
                )
            except QuotaExceeded as exc:
                # Over quota before any spend: nothing ran, so this request has no api_call rows.
                # Mark the run refused (in this same committed session) and return an explicit 429
                # with a machine-readable code, never a silent grant. check_and_reserve_quota has
                # already emitted the quota.exceeded billing event, so it is not emitted again.
                await metering.finish_analysis_run(
                    _msession,
                    _met_run_id,
                    status="refused",
                    error=str(exc),
                )
                cascade_tracker.fail_run(run, "quota", str(exc))
                await emit(
                    "analysis.run.refused",
                    module="polarius",
                    level="warn",
                    pid=str(project_id),
                    reason="quota.exceeded",
                )
                return JSONResponse(
                    status_code=exc.status_code,
                    content={
                        "detail": {"code": exc.code, "metric": exc.metric, "limit": exc.limit}
                    },
                )

    # ── Run the cascade ───────────────────────────────────────────────────────────────────────────
    # `run_analysis_job` (analysis/tasks.py) is the single cascade implementation. It runs the
    # (possibly limited) layers, builds the L2, L3 and L4 sections, records failures and refunds
    # quota, emits the completion events, closes the metered run, records usage and returns the
    # AnalyzeResponse. Everything above (resource gate, compliance gate, entitlements and quota
    # reservation) is resolved here and passed in as a CascadeJob.
    #
    # The cascade always runs inside this request. The analyze_async setting is accepted but not
    # acted on yet: there is no broker, and the task wrapper `run_analysis_task` bridges through
    # asyncio.run, which cannot nest inside this running loop. Returning 202 and handing the job
    # to a worker process is future work.
    job = CascadeJob(
        project_id=project_id,
        layers=_layers,
        as_of=as_of,
        max_concurrency=req.max_concurrency,
        limit=req.limit,
        force=req.force,
        tracker_run=run,
        law_source=law_source,
        met_run_id=_met_run_id,
        met_org_id=_met_org_id,
        met_reserved=_met_reserved,
        met_note=_met_note,
    )
    return await run_analysis_job(job)


class FindingOut(BaseModel):
    id: UUID
    finding_type: str
    statement: str
    severity: str
    cited_provisions: list[UUID]
    cited_documents: list[UUID]
    confidence_overall: float
    analysis_layer: str
    generated_by: str
    review_state: str
    payload: dict[str, Any]


class FindingsResponse(BaseModel):
    project_id: UUID
    n_findings: int
    findings: list[FindingOut]


@router.get("/api/projects/{project_id}/findings", response_model=FindingsResponse)
async def project_findings(
    project_id: UUID,
    layer: str | None = None,
    limit: int = 200,
) -> FindingsResponse:
    """List a project's findings, newest first, optionally filtered by `analysis_layer`."""
    async with session_scope() as session:
        stmt = (
            select(Finding)
            .where(Finding.project_id == project_id)
            .order_by(desc(Finding.created_at))
            .limit(limit)
        )
        if layer:
            stmt = stmt.where(Finding.analysis_layer == layer)
        rows = (await session.execute(stmt)).scalars().all()
    return FindingsResponse(
        project_id=project_id,
        n_findings=len(rows),
        findings=[
            FindingOut(
                id=f.id,
                finding_type=f.finding_type,
                statement=f.statement,
                severity=f.severity,
                cited_provisions=list(f.cited_provisions),
                cited_documents=list(f.cited_documents),
                confidence_overall=float(f.confidence_overall),
                analysis_layer=f.analysis_layer,
                generated_by=f.generated_by,
                review_state=f.review_state,
                payload=f.payload,
            )
            for f in rows
        ],
    )
