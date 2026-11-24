"""Plain-language compliance digest for a company profile (read-only).

Reads a project's findings and cited provisions and shapes them, through the pure
`analysis/render.py`, into an obligation register, a checklist, a gap report and an action list.
Every response carries the Czech disclaimer and a `has_unreviewed_citations` flag. All routes
return 404 unless `digest_enabled` is set (off by default).
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from regsuper.analysis import render
from regsuper.api.export import resolve_cited_provisions
from regsuper.config import get_settings
from regsuper.entitlements import metering
from regsuper.storage.database import session_scope
from regsuper.storage.models import Finding, Project

router = APIRouter(prefix="/api/digest", tags=["digest"])

Language = Literal["cs", "en"]


# ---------------------------------------------------------------------------
# Item schemas: the plain-language vocabulary. The Czech literals are the user-facing labels,
# pinned here so render.py and the frontend agree.
# ---------------------------------------------------------------------------
class ObligationItem(BaseModel):
    modal_label: str
    actor_label: str
    deadline: str | None = None
    legal_source: str
    legal_source_url: str | None = None
    cited_provision_id: UUID
    confidence: float
    review_state: str
    # Whether the obligation binds this entity in its role. The defaults ('yes', '') apply to
    # findings the scoring pass did not touch (for example with entity_scoping_enabled off).
    binds: Literal["yes", "conditional", "out_of_scope"] = "yes"
    binds_reason: str = ""
    # Applicability confidence (empty unless applicability_confidence_enabled).
    confidence_label: str = ""
    confidence_reason: str = ""
    # All grounded citations (empty unless digest_citation_strategy is not 'first_only').
    all_cited_refs: list[dict] = Field(default_factory=list)
    # Multi-limb and de-duplication annotations (empty unless multi_limb_enabled).
    wf4: dict = Field(default_factory=dict)
    # Temporal-freshness verdicts (empty unless temporal_freshness_enabled).
    temporal: dict = Field(default_factory=dict)


class ChecklistItem(BaseModel):
    obligation: ObligationItem
    status: Literal["splněno", "nesplněno", "neověřeno", "nejasné"]


class GapItem(BaseModel):
    statement: str
    severity_label: Literal["kritické", "důležité", "doporučené"]
    legal_source: str
    cited_provision_id: UUID
    # Why the gap was demoted (out of scope, duplicate, stale); empty for a normal gap.
    suppressed_by: list[str] = Field(default_factory=list)


class ActionItem(BaseModel):
    statement: str
    priority: Literal["okamžitě", "do 30 dnů", "do 90 dnů", "průběžně"]
    legal_source: str


# ---------------------------------------------------------------------------
# Response envelopes; each carries the disclaimer and has_unreviewed_citations.
# ---------------------------------------------------------------------------
class _DigestBase(BaseModel):
    profile_id: UUID
    project_id: UUID
    lang: Language = "cs"
    disclaimer: str = ""
    has_unreviewed_citations: bool = False
    # The answer mode of the org's plan tier (Lite: 'information', Business and Advisor:
    # 'compliance'), so the frontend renders the right depth of presentation. None when
    # entitlements are disabled. Presentation only: it changes no finding and hides no obligation.
    tier_answer_mode: str | None = None


class ObligationRegisterResponse(_DigestBase):
    # ``obligations`` is the binding register: unconditional and conditional duties (the latter
    # carry binds='conditional' and binds_reason, so the frontend can say "binds you only if...").
    # Duties judged not to bind this entity (binds='out_of_scope') move to ``does_not_bind``: shown
    # for transparency, never dropped. With entity_scoping_enabled off, nothing is out of scope.
    obligations: list[ObligationItem] = Field(default_factory=list)
    does_not_bind: list[ObligationItem] = Field(default_factory=list)


class ComplianceChecklistResponse(_DigestBase):
    items: list[ChecklistItem] = Field(default_factory=list)


class GapReportResponse(_DigestBase):
    gaps: list[GapItem] = Field(default_factory=list)
    # Gaps demoted to a visible context section (out of scope, duplicate or stale). None is ever
    # dropped: every gap is in `gaps` or `context_gaps`. Empty unless a gap-suppression flag is on.
    context_gaps: list[GapItem] = Field(default_factory=list)


class ActionListResponse(_DigestBase):
    actions: list[ActionItem] = Field(default_factory=list)


class DigestSummaryResponse(_DigestBase):
    """All four digest views in one response, capped at 50 items per category."""

    obligations: list[ObligationItem] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    gaps: list[GapItem] = Field(default_factory=list)
    actions: list[ActionItem] = Field(default_factory=list)
    does_not_bind: list[ObligationItem] = Field(
        default_factory=list
    )  # out-of-scope duties, demoted rather than dropped
    context_gaps: list[GapItem] = Field(default_factory=list)  # gaps demoted rather than dropped


# ---------------------------------------------------------------------------
# Feature flag (same pattern as export.py._require_enabled). A 404 rather than a 403, so a
# disabled route is indistinguishable from a missing one.
# ---------------------------------------------------------------------------
def _require_enabled() -> None:
    """Raise 404 unless ``digest_enabled`` is set (it is off by default)."""
    if not get_settings().digest_enabled:
        raise HTTPException(status_code=404, detail="not found")


# ---------------------------------------------------------------------------
# Pure mappers from ``render`` dataclasses to the response models (no database access). Each
# coerces the string ``cited_provision_id`` to a ``UUID``.
# ---------------------------------------------------------------------------
def _to_obligation_item(r: render.ObligationItem) -> ObligationItem:
    """Map a :class:`render.ObligationItem` to the response model.

    The ``UUID(r.cited_provision_id)`` coercion is safe because the id comes from
    ``Finding.cited_provisions``, an ``ARRAY(PG_UUID)`` column, so every id (app or engine corpus)
    is a real UUID. ``build_digest`` does not care about the id's shape; if the column were ever
    changed to text, this coercion would fail with a 500, so change the response schema to
    ``str`` at the same time.
    """
    return ObligationItem(
        modal_label=r.modal_label,
        actor_label=r.actor_label,
        deadline=r.deadline,
        legal_source=r.legal_source,
        legal_source_url=r.legal_source_url,
        cited_provision_id=UUID(r.cited_provision_id),
        confidence=r.confidence,
        review_state=r.review_state,
        binds=getattr(r, "binds", "yes"),
        binds_reason=getattr(r, "binds_reason", ""),
        confidence_label=getattr(r, "confidence_label", ""),
        confidence_reason=getattr(r, "confidence_reason", ""),
        all_cited_refs=[dict(x) for x in getattr(r, "all_cited_refs", ()) or ()],
        wf4=dict(getattr(r, "wf4", {}) or {}),
        temporal=dict(getattr(r, "temporal", {}) or {}),
    )


def _to_checklist_item(obl_render: render.ObligationItem, status: str) -> ChecklistItem:
    """Pair a render obligation (as a nested :class:`ObligationItem`) with its status."""
    return ChecklistItem(obligation=_to_obligation_item(obl_render), status=status)


def _to_gap_item(r: render.GapItem) -> GapItem:
    """Map a :class:`render.GapItem` to the response model."""
    return GapItem(
        statement=r.statement,
        severity_label=r.severity_label,
        legal_source=r.legal_source,
        cited_provision_id=UUID(r.cited_provision_id),
        suppressed_by=list(getattr(r, "suppressed_by", ()) or ()),
    )


def _to_action_item(r: render.ActionItem) -> ActionItem:
    """Map a :class:`render.ActionItem` to the response model (``action`` becomes ``statement``)."""
    return ActionItem(
        statement=r.action,
        priority=r.priority,
        legal_source=r.legal_source,
    )


def _dominant_answer_mode(findings: list[Finding]) -> str:
    """Return one deterministic answer mode for the digest (same rule as export.py).

    'reserved_advice' if any finding is reserved advice, else 'needs_review' if any needs review,
    else 'information'. Independent of finding order; a missing column reads as None.
    """
    modes = {getattr(f, "answer_mode", None) for f in findings}
    if "reserved_advice" in modes:
        return "reserved_advice"
    if "needs_review" in modes:
        return "needs_review"
    return "information"


def _disclaimer_text(block: render.DisclaimerBlock | None, lang: Language) -> str:
    """Return the disclaimer text in ``lang`` (empty when there is no block)."""
    if block is None:
        return ""
    return block.text_cs if lang == "cs" else block.text_en


# ---------------------------------------------------------------------------
# Read-only data access: resolve the project for a profile (an explicit id must belong to the
# profile, otherwise the latest run is used), load its findings and cited provisions, and shape
# them through the pure ``render`` layer. Nothing is written.
# ---------------------------------------------------------------------------
async def _resolve_project(session, profile_id: UUID, project_id: UUID | None) -> Project:
    """Resolve the project to digest, or raise 404.

    If ``project_id`` is given it must belong to ``profile_id``; otherwise the profile's latest
    run is used. Raises 404 if neither yields a project.
    """
    if project_id is not None:
        project = await session.get(Project, project_id)
        if project is None or project.business_profile_id != profile_id:
            raise HTTPException(status_code=404, detail="project not found for this profile")
        return project
    project = (
        (
            await session.execute(
                select(Project)
                .where(Project.business_profile_id == profile_id)
                .order_by(desc(Project.created_at))
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="no analysis run for this profile")
    return project


async def _tier_answer_mode(session, project: Project) -> str | None:
    """Return the answer mode of the project org's plan tier, or None when entitlements are off.

    Resolved fail-closed through the shared metering helper. Presentation only: it gates and
    hides nothing.
    """
    if not metering.entitlements_on():
        return None
    org_id = getattr(project, "org_id", None)
    if org_id is None:
        return None
    ent = await metering.resolve_for_entry(org_id, session=session)
    return metering.answer_mode_for(ent)


async def _load_digest(session, project: Project) -> render.Digest:
    """Load the project's findings and cited provisions and build the digest."""
    findings = list(
        (await session.execute(select(Finding).where(Finding.project_id == project.id)))
        .scalars()
        .all()
    )
    # Resolve cited provisions from the right corpus: the app's provision table for app findings,
    # the live engine for engine-corpus findings. build_digest drops a finding whose provision is
    # missing, so this keeps current-law obligations in the register. For an app-only project the
    # engine is never called. Findings only come from the engine corpus when the cascade ran with
    # law_source='nexus', which require_cascade_law_source currently refuses.
    provisions = list((await resolve_cited_provisions(session, findings)).values())
    return render.build_digest(
        findings,
        provisions,
        answer_mode=_dominant_answer_mode(findings),
        citation_strategy=getattr(get_settings(), "digest_citation_strategy", "first_only"),
        multi_limb=bool(getattr(get_settings(), "multi_limb_enabled", False)),
        # Pass a reference date only when temporal annotation or temporal gap suppression is on
        # (both off by default). The clock is read here so build_digest stays pure; the live digest
        # is not a hashed artifact, so re-evaluating freshness each day is fine.
        temporal_as_of=(
            date.today()
            if (
                getattr(get_settings(), "temporal_freshness_enabled", False)
                or getattr(get_settings(), "temporal_gap_suppression_enabled", False)
            )
            else None
        ),
        temporal_gap_suppression=bool(
            getattr(get_settings(), "temporal_gap_suppression_enabled", False)
        ),
    )


@router.get("/{profile_id}/obligations", response_model=ObligationRegisterResponse)
async def obligations(
    profile_id: UUID,
    lang: Language = "cs",
    project_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ObligationRegisterResponse:
    """Return the plain-language obligation register for a profile.

    Uses the profile's latest analysis unless `project_id` names one of its projects. Paged with
    `limit` and `offset`; `does_not_bind` lists duties judged not to apply and is capped at
    `limit` without paging. Returns 404 if the profile has no matching project. Intended plan
    tier: Lite and above (not enforced yet).
    """
    _require_enabled()
    async with session_scope() as session:
        project = await _resolve_project(session, profile_id, project_id)
        digest = await _load_digest(session, project)
        tier_am = await _tier_answer_mode(session, project)
    items = [_to_obligation_item(r) for r in digest.obligations][offset : offset + limit]
    # does_not_bind is a supplementary, collapsed section: cap it at `limit` from the start rather
    # than paging it with the register, so paging the obligations never shifts or hides it.
    does_not_bind = [_to_obligation_item(r) for r in digest.does_not_bind][:limit]
    return ObligationRegisterResponse(
        profile_id=profile_id,
        project_id=project.id,
        lang=lang,
        disclaimer=_disclaimer_text(digest.disclaimer, lang),
        has_unreviewed_citations=digest.has_unreviewed_citations,
        tier_answer_mode=tier_am,
        obligations=items,
        does_not_bind=does_not_bind,
    )


@router.get("/{profile_id}/checklist", response_model=ComplianceChecklistResponse)
async def checklist(
    profile_id: UUID,
    lang: Language = "cs",
    project_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ComplianceChecklistResponse:
    """Return the compliance checklist: each obligation with its fulfilment status.

    Intended plan tier: Business and above (not enforced yet).
    """
    _require_enabled()
    async with session_scope() as session:
        project = await _resolve_project(session, profile_id, project_id)
        digest = await _load_digest(session, project)
        tier_am = await _tier_answer_mode(session, project)
    items = [
        _to_checklist_item(digest.obligations[i], digest.checklist[i].status)
        for i in range(len(digest.checklist))
    ][offset : offset + limit]
    return ComplianceChecklistResponse(
        profile_id=profile_id,
        project_id=project.id,
        lang=lang,
        disclaimer=_disclaimer_text(digest.disclaimer, lang),
        has_unreviewed_citations=digest.has_unreviewed_citations,
        tier_answer_mode=tier_am,
        items=items,
    )


@router.get("/{profile_id}/gap-report", response_model=GapReportResponse)
async def gap_report(
    profile_id: UUID,
    lang: Language = "cs",
    project_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> GapReportResponse:
    """Return the compliance gaps, most critical first.

    Demoted gaps are listed separately in `context_gaps`. Intended plan tier: Business and above
    (not enforced yet).
    """
    _require_enabled()
    async with session_scope() as session:
        project = await _resolve_project(session, profile_id, project_id)
        digest = await _load_digest(session, project)
        tier_am = await _tier_answer_mode(session, project)
    items = [_to_gap_item(r) for r in digest.gaps][offset : offset + limit]
    context = [_to_gap_item(r) for r in digest.context_gaps][
        :limit
    ]  # supplementary; capped, not paged
    return GapReportResponse(
        profile_id=profile_id,
        project_id=project.id,
        lang=lang,
        disclaimer=_disclaimer_text(digest.disclaimer, lang),
        has_unreviewed_citations=digest.has_unreviewed_citations,
        tier_answer_mode=tier_am,
        gaps=items,
        context_gaps=context,
    )


@router.get("/{profile_id}/actions", response_model=ActionListResponse)
async def actions(
    profile_id: UUID,
    lang: Language = "cs",
    project_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> ActionListResponse:
    """Return the prioritised action list. Intended plan tier: Business and above (not enforced)."""
    _require_enabled()
    async with session_scope() as session:
        project = await _resolve_project(session, profile_id, project_id)
        digest = await _load_digest(session, project)
        tier_am = await _tier_answer_mode(session, project)
    items = [_to_action_item(r) for r in digest.actions][offset : offset + limit]
    return ActionListResponse(
        profile_id=profile_id,
        project_id=project.id,
        lang=lang,
        disclaimer=_disclaimer_text(digest.disclaimer, lang),
        has_unreviewed_citations=digest.has_unreviewed_citations,
        tier_answer_mode=tier_am,
        actions=items,
    )


@router.get("/{profile_id}/summary", response_model=DigestSummaryResponse)
async def summary(
    profile_id: UUID,
    lang: Language = "cs",
    project_id: UUID | None = None,
) -> DigestSummaryResponse:
    """Return all four digest views in one response, capped at 50 items per category.

    Intended plan tier: Business and above (not enforced yet).
    """
    _require_enabled()
    async with session_scope() as session:
        project = await _resolve_project(session, profile_id, project_id)
        digest = await _load_digest(session, project)
        tier_am = await _tier_answer_mode(session, project)
    return DigestSummaryResponse(
        profile_id=profile_id,
        project_id=project.id,
        lang=lang,
        disclaimer=_disclaimer_text(digest.disclaimer, lang),
        has_unreviewed_citations=digest.has_unreviewed_citations,
        tier_answer_mode=tier_am,
        obligations=[_to_obligation_item(r) for r in digest.obligations][:50],
        checklist=[
            _to_checklist_item(digest.obligations[i], digest.checklist[i].status)
            for i in range(len(digest.checklist))
        ][:50],
        gaps=[_to_gap_item(r) for r in digest.gaps][:50],
        actions=[_to_action_item(r) for r in digest.actions][:50],
        does_not_bind=[_to_obligation_item(r) for r in digest.does_not_bind][:50],
        context_gaps=[_to_gap_item(r) for r in digest.context_gaps][:50],
    )
