"""Lawyer hand-off: a referral offered instead of an answer when a question needs reserved advice.

An offer row carries no personal data. The user's contact details are written only after consent
has been recorded (GDPR), which `_write_contact_payload` enforces in application code. Referrals
are recorded but not yet transmitted to any partner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from regsuper.api.auth import get_principal
from regsuper.observability.events import emit
from regsuper.storage.audit import log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import Handoff

router = APIRouter(prefix="/api/handoff", tags=["handoff"])

# What may open a hand-off: a reserved-advice refusal, an answer below the confidence threshold
# that needs review, or an explicit user request.
HandoffTrigger = Literal["reserved_advice", "below_confidence", "user_requested"]
_VALID_TRIGGERS: frozenset[str] = frozenset(
    ("reserved_advice", "below_confidence", "user_requested")
)

HandoffStatus = Literal["offered", "accepted", "sent", "closed", "declined"]


class HandoffConsentError(RuntimeError):
    """Raised when ``contact_payload`` would be written while ``consent_at`` is NULL.

    Enforces the consent-first rule; the caller's transaction is aborted before any personal data
    reaches the database.
    """


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------
class HandoffCreateRequest(BaseModel):
    """Body for creating a referral offer; it carries no contact details.

    ``trigger`` is why the referral was offered. ``source_query`` is the (truncated) user question,
    kept as context for the lawyer. Contact details are supplied later, at /consent.
    """

    trigger: HandoffTrigger
    project_id: UUID | None = None
    reason: str | None = None
    source_query: str | None = None


class ContactPayload(BaseModel):
    """The user's contact details (personal data), accepted only at /consent."""

    name: str
    email: str
    phone: str | None = None


class HandoffConsentRequest(BaseModel):
    """Body for /consent: consent is recorded, then the contact details are stored."""

    contact_payload: ContactPayload


class HandoffResponse(BaseModel):
    id: UUID
    project_id: UUID | None = None
    trigger: str
    status: HandoffStatus
    reason: str | None = None
    answer_mode: str
    consent_at: datetime | None = None
    has_contact: bool = False  # whether contact details exist; the details are never returned
    created_at: datetime | None = None


def _to_response(row: Handoff) -> HandoffResponse:
    """Map a row to the response model, exposing only whether contact details were captured."""
    return HandoffResponse(
        id=row.id,
        project_id=row.project_id,
        trigger=row.trigger,
        status=row.status,  # type: ignore[arg-type]
        reason=row.reason,
        answer_mode=row.answer_mode,
        consent_at=row.consent_at,
        has_contact=bool(row.contact_payload),
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Core helpers: the offer and consent steps, in the order GDPR requires
# ---------------------------------------------------------------------------
def _write_contact_payload(row: Handoff, contact_payload: dict[str, Any]) -> None:
    """Write ``contact_payload``; the only code path that does so.

    A non-empty payload is accepted only when ``consent_at`` is already set. Otherwise
    :class:`HandoffConsentError` is raised, the payload stays empty and the transaction is aborted
    before any personal data is stored. Writing an empty payload is always allowed (it clears it).
    """
    if contact_payload and row.consent_at is None:
        raise HandoffConsentError(
            "refusing to write handoff.contact_payload while consent_at is NULL — "
            "PII may be stored ONLY on or after consent (GDPR, failure mode-7). "
            "Set consent_at first (record_handoff_consent)."
        )
    row.contact_payload = dict(contact_payload)


async def create_handoff_offer(
    session: AsyncSession,
    *,
    trigger: str,
    reason: str,
    answer_mode: str,
    project_id: UUID | None = None,
    org_id: UUID | None = None,
    source_query: str | None = None,
    cited_provisions: list[UUID] | None = None,
) -> Handoff:
    """Create a hand-off offer; called by the input compliance gate and reserved-advice findings.

    The row starts with ``status='offered'``, an empty ``contact_payload`` and a NULL
    ``consent_at``, so it holds no personal data until the user consents. ``org_id`` is stamped so
    the row is tenant-attributed once row-level security is enforced. A ``handoff.created`` audit
    event is written in the same session.

    Does not commit: the caller owns the transaction, so the offer shares a unit of work with the
    refusal or finding that produced it.
    """
    if trigger not in _VALID_TRIGGERS:
        raise ValueError(
            f"invalid handoff trigger {trigger!r}; expected one of {sorted(_VALID_TRIGGERS)}"
        )
    row = Handoff(
        project_id=project_id,
        org_id=org_id,
        trigger=trigger,
        answer_mode=answer_mode,
        reason=reason,
        source_query=source_query,
        cited_provisions=cited_provisions or [],
        status="offered",
        contact_payload={},  # no personal data before consent (GDPR)
        consent_at=None,
    )
    session.add(row)
    await session.flush()  # populate row.id for the audit row and the response

    await log_event(
        session,
        actor_type="system",
        action="handoff.created",
        target_type="handoff",
        target_id=str(row.id),
        project_id=project_id,
        org_id=org_id,
        payload={"trigger": trigger, "answer_mode": answer_mode},
    )
    await emit(
        "handoff.created",
        project_id=str(project_id) if project_id else None,
        module="polarius",
        trigger=trigger,
        handoff_id=str(row.id),
        name=reason,
    )
    return row


async def record_handoff_consent(
    session: AsyncSession,
    *,
    handoff_id: UUID,
    contact_payload: dict[str, Any],
) -> Handoff:
    """Record consent on an existing offer, then store the contact details.

    ``consent_at`` is set before ``contact_payload`` is written, so the consent guard always sees
    it. Consenting again keeps the original timestamp and updates the contact details. Raises
    :class:`LookupError` for an unknown id. Does not commit: the caller owns the transaction.
    """
    row = await session.get(Handoff, handoff_id)
    if row is None:
        raise LookupError(f"handoff {handoff_id} not found")

    # Set consent_at first so the payload guard passes, then write the personal data.
    if row.consent_at is None:
        row.consent_at = datetime.now(UTC)
    _write_contact_payload(row, contact_payload)
    if row.status == "offered":
        row.status = "accepted"
    row.updated_at = datetime.now(UTC)

    await log_event(
        session,
        actor_type="user",
        action="handoff.consent.recorded",
        target_type="handoff",
        target_id=str(row.id),
        project_id=row.project_id,
        org_id=row.org_id,
        payload={"trigger": row.trigger},
    )
    await emit(
        "handoff.consent.recorded",
        project_id=str(row.project_id) if row.project_id else None,
        module="polarius",
        handoff_id=str(row.id),
    )
    return row


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.post("", response_model=HandoffResponse, status_code=201)
async def create_handoff(req: HandoffCreateRequest, request: Request) -> HandoffResponse:
    """Create a referral offer with status `offered`; it carries no contact details."""
    p = await get_principal(request)
    async with session_scope() as session:
        row = await create_handoff_offer(
            session,
            trigger=req.trigger,
            reason=req.reason or "lawful-lane referral",
            answer_mode="reserved_advice" if req.trigger == "reserved_advice" else "needs_review",
            project_id=req.project_id,
            org_id=p.org_id,
            source_query=req.source_query,
        )
        return _to_response(row)


@router.post("/{handoff_id}/consent", response_model=HandoffResponse)
async def consent_handoff(
    handoff_id: UUID, req: HandoffConsentRequest, request: Request
) -> HandoffResponse:
    """Record the user's consent and contact details for a referral.

    Consent is timestamped before the contact details are stored, and an `offered` referral
    becomes `accepted`. Consenting again keeps the original timestamp. Returns 404 for an unknown
    referral. The response never includes the contact details.
    """
    await get_principal(request)  # fail closed when auth is required
    async with session_scope() as session:
        try:
            row = await record_handoff_consent(
                session,
                handoff_id=handoff_id,
                contact_payload=req.contact_payload.model_dump(),
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="handoff not found") from None
        return _to_response(row)


@router.get("/{handoff_id}", response_model=HandoffResponse)
async def get_handoff(handoff_id: UUID, request: Request) -> HandoffResponse:
    """Return one referral (without contact details). Returns 404 if it does not exist."""
    await get_principal(request)
    async with session_scope() as session:
        row = await session.get(Handoff, handoff_id)
        if row is None:
            raise HTTPException(status_code=404, detail="handoff not found")
        return _to_response(row)


@router.get("", response_model=list[HandoffResponse])
async def list_handoffs(request: Request, project_id: UUID | None = None) -> list[HandoffResponse]:
    """List referrals, newest first, optionally filtered by `project_id`."""
    await get_principal(request)
    async with session_scope() as session:
        stmt = select(Handoff).order_by(desc(Handoff.created_at))
        if project_id is not None:
            stmt = stmt.where(Handoff.project_id == project_id)
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_response(r) for r in rows]
