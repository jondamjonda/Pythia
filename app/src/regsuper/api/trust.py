"""EU AI Act trust posture: public transparency disclosures, human review and incident logging.

The public GETs are pure projections of configuration (no database, no side effects), so a trust
page works even when the database is down. The high-risk classification is a human, legal
decision: it is the configured string ``settings.ai_act_high_risk_classification``, returned
verbatim and never computed, derived or branched on in code.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from regsuper import __version__
from regsuper.analysis.model_resolver import ModelRole, resolve_model
from regsuper.api.admin import require_control_token
from regsuper.api.auth import get_principal
from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.storage.audit import log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import Finding

router = APIRouter(tags=["trust"])

# Fixed statement of intended purpose for the Art. 11 technical documentation: the product is a
# compliance-information tool for Czech SMEs, not an advice engine.
_PURPOSE = (
    "Polarius is an AI-powered legal-information tool that helps Czech SMEs locate, "
    "read, and understand the regulatory provisions that apply to their activity, "
    "always grounded in cited primary sources. It surfaces obligations and source "
    "law; it does not give individualised legal advice."
)

# Human-oversight description for the technical documentation (Art. 14): anyone can request
# review of a finding, the operator can engage the kill switch, and the product refuses rather
# than guesses on reserved legal advice or low confidence.
_HUMAN_OVERSIGHT = (
    "Every finding can be flagged for human review via "
    "POST /api/findings/{id}/request-review (Art. 14). The operator can halt all "
    "generation immediately via the control-room kill-switch. The product refuses "
    "to answer — and offers a lawyer hand-off — when a query reaches reserved legal "
    "advice or falls below the confidence floor, rather than producing an "
    "unsupported answer."
)

# Known limitations disclosed for transparency (Art. 13). Written by hand, not measured.
_KNOWN_LIMITATIONS: list[str] = [
    "Coverage is limited to the ingested corpus (Czech legislation and selected EU "
    "instruments); acts or amendments outside the corpus are not reasoned over.",
    "Outputs are point-in-time against the corpus as ingested and may lag very recent amendments.",
    "The tool provides legal information, not legal advice; it does not replace a "
    "qualified lawyer.",
    "Findings are model-generated from cited sources and can contain errors; the "
    "citation is the load-bearing artifact and should be verified.",
    "Reserved legal advice and out-of-scope questions are refused, not answered.",
]

_INTENDED_USE = (
    "Self-service regulatory orientation for Czech small and medium enterprises and "
    "their advisers: finding which provisions apply, what obligations they impose, "
    "and where the source law is."
)

_OUT_OF_SCOPE = (
    "Individualised legal advice, litigation strategy, representation, drafting of "
    "binding legal opinions, and any use as the sole basis for a legal or regulatory "
    "decision without qualified human review."
)


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------
class ReviewRequest(BaseModel):
    """Body of a human-review request (Art. 14): a short reason without personal data."""

    reason: str = Field(default="", max_length=500)


class ReviewResponse(BaseModel):
    finding_id: UUID
    review_state: str


class IncidentBody(BaseModel):
    """Post-market incident report (Art. 72).

    ``severity`` is the only enumerated field; the assessment fields are free text written by the
    operator or legal team.
    """

    description: str
    severity: Literal["serious", "non_serious"]
    affected_project_id: str | None = None
    operator_assessment: str
    corrective_action: str | None = None


class IncidentResponse(BaseModel):
    incident_id: UUID
    status: str


# ---------------------------------------------------------------------------
# Config-derived projections (pure; no DB, no side effects)
# ---------------------------------------------------------------------------
def _inference_provider(settings: Any) -> str:
    """Derive the inference provider's name from the configured reasoning model.

    ``claude-*`` models map to Anthropic; everything else, including ``gpt-*`` and the o-series,
    maps to OpenAI.
    """
    model = resolve_model(ModelRole.OBLIGATIONS)
    if model.startswith("claude"):
        return "Anthropic"
    if model.startswith(("gpt", "o1", "o3", "o4")):
        return "OpenAI"
    return "OpenAI"


def _data_residency(settings: Any) -> dict[str, Any]:
    """Build the data-residency disclosure (Art. 13).

    The technical documentation embeds the same object, so the two never drift.
    ``training_on_user_data`` is always False because the DPA forbids training on customer data.
    """
    return {
        "compute_region": settings.data_residency_compute_region,
        "storage_region": settings.data_residency_storage_region,
        "model_inference_provider": _inference_provider(settings),
        "training_on_user_data": False,
        "dpa_reference": settings.openai_dpa_url,
    }


# ---------------------------------------------------------------------------
# Public transparency GETs (no auth: these are disclosures)
# ---------------------------------------------------------------------------
@router.get("/api/trust/data-residency")
async def trust_data_residency() -> dict[str, Any]:
    """Disclose where compute and storage run and which provider performs inference.

    Also states that user data is never used for training (EU AI Act Art. 13). Public.
    """
    return _data_residency(get_settings())


@router.get("/api/trust/disclosure")
async def trust_disclosure() -> dict[str, Any]:
    """Return the "this is AI, not legal advice" notice (EU AI Act Art. 13). Public."""
    settings = get_settings()
    return {
        "product": "Polarius",
        "nature": "AI-powered legal-information tool",
        "not_legal_advice": True,
        "model": resolve_model(ModelRole.OBLIGATIONS),
        "human_oversight_url": "/api/findings/{id}/request-review",
        "dpa_url": settings.openai_dpa_url,
    }


@router.get("/api/trust/ai-act/technical-doc")
async def trust_ai_act_technical_doc() -> dict[str, Any]:
    """Return the technical documentation summary (EU AI Act Art. 11 and Annex IV). Public.

    ``high_risk_classification`` is the classification configured by the operator's legal
    review, returned verbatim.
    """
    settings = get_settings()
    return {
        "product": "Polarius",
        "version": __version__,
        "purpose": _PURPOSE,
        # The configured string, verbatim; never derived in code.
        "high_risk_classification": settings.ai_act_high_risk_classification,
        "intended_use": _INTENDED_USE,
        "out_of_scope": _OUT_OF_SCOPE,
        "known_limitations": _KNOWN_LIMITATIONS,
        "human_oversight": _HUMAN_OVERSIGHT,
        "data_residency": _data_residency(settings),
        "contact": settings.operator_contact_email,
    }


# ---------------------------------------------------------------------------
# Human oversight (Art. 14): flag a finding for human review (authenticated)
# ---------------------------------------------------------------------------
@router.post("/api/findings/{finding_id}/request-review", response_model=ReviewResponse)
async def request_finding_review(
    finding_id: UUID, request: Request, body: ReviewRequest = Body(default=ReviewRequest())
) -> ReviewResponse:
    """Flag a finding for human review (EU AI Act Art. 14, human oversight).

    Idempotent: an ``unreviewed`` finding moves to ``review_requested``, and a finding in any other
    state is returned unchanged without error. Returns 404 if the finding does not exist.
    """
    p = await get_principal(request)  # fail closed when auth is required
    async with session_scope() as session:
        row = await session.get(Finding, finding_id)
        if row is None:
            raise HTTPException(status_code=404, detail="finding not found")

        # Only the first transition from 'unreviewed' changes state and is audited, in the same
        # transaction as the state change.
        if row.review_state == "unreviewed":
            row.review_state = "review_requested"
            await log_event(
                session,
                actor_type="user",
                actor_id=str(p.user_id),
                action="finding.review_requested",
                target_type="finding",
                target_id=str(finding_id),
                project_id=row.project_id,
                org_id=row.org_id,
                payload={"reason": (body.reason or "")[:500]},
            )
            await emit(
                "finding.review_requested",
                project_id=str(row.project_id) if row.project_id else None,
                module="polarius",
                finding_id=str(finding_id),
            )

        return ReviewResponse(finding_id=finding_id, review_state=row.review_state)


# ---------------------------------------------------------------------------
# Post-market incident log (Art. 72); admin only, fails closed
# ---------------------------------------------------------------------------
@router.post(
    "/admin/api/aiact/incident",
    response_model=IncidentResponse,
    tags=["admin"],
)
async def log_aiact_incident(
    body: IncidentBody, _: None = Depends(require_control_token)
) -> IncidentResponse:
    """Record a post-market incident (EU AI Act Art. 72).

    Requires the control token, like every ``/admin/api`` endpoint, and fails closed: outside
    local dev, a deployment with no token configured refuses the request. The incident is appended
    to the audit trail and announced on the control-room event channel.
    """
    async with session_scope() as session:
        # A fresh incident id (the audit target), generated here so the response can return it
        # and the event can reference it.
        incident_id = uuid4()
        payload: dict[str, Any] = {
            "incident_id": str(incident_id),
            "description": body.description,
            "severity": body.severity,
            "affected_project_id": body.affected_project_id,
            "operator_assessment": body.operator_assessment,
            "corrective_action": body.corrective_action,
        }
        await log_event(
            session,
            actor_type="operator",
            action="aiact.incident",
            target_type="aiact_incident",
            target_id=str(incident_id),
            payload=payload,
        )
        await emit(
            "aiact.incident.logged",
            module="polarius",
            level="error" if body.severity == "serious" else "warn",
            severity=body.severity,
            incident_id=str(incident_id),
        )
    return IncidentResponse(incident_id=incident_id, status="logged")
