"""Append-only audit log, written for every meaningful system action.

The `audit_event` table is the single source of truth for which provisions were retrieved for
which finding, which prompt produced which model output, and which human overrode which system
claim. The database trigger `trg_audit_event_no_update` blocks UPDATE and DELETE, and this
module is the only intended write path.

The module also enforces, at insert time, that every finding cites at least one provision and
its source documents.
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from regsuper.storage.models import AuditEvent, Finding


async def log_event(
    session: AsyncSession,
    *,
    actor_type: str,
    action: str,
    target_type: str,
    target_id: str,
    actor_id: str | None = None,
    project_id: UUID | None = None,
    model_version: str | None = None,
    prompt_hash: str | None = None,
    payload: dict[str, Any] | None = None,
    org_id: str | UUID | None = None,
) -> None:
    """Add one audit_event row to the session without committing; the caller owns the transaction.

    ``org_id`` sets the row's tenant column. When it is None (the default) the column stays
    NULL; such rows are visible to platform admins only (`audit_event.org_id` is nullable).
    This only ever inserts; append-only enforcement is left to the database trigger.
    """
    evt = AuditEvent(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        project_id=project_id,
        model_version=model_version,
        prompt_hash=prompt_hash,
        payload=payload or {},
        org_id=org_id,
    )
    session.add(evt)


def hash_prompt(prompt: str) -> str:
    """SHA-256 hex digest, used to identify exact prompt templates in audit rows."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Citation enforcement
#
# A model-produced finding without a citation is a critical failure, so it is rejected when the
# row is inserted rather than relying on the prompt. The schema already has a CHECK constraint
# on cited_provisions; this hook fails earlier, in Python, with a clear message, and also
# requires cited_documents to be non-empty, which the CHECK does not cover.
# ---------------------------------------------------------------------------
class EmptyCitationError(ValueError):
    """Raised when a finding is inserted without citations."""


@event.listens_for(Finding, "before_insert", propagate=True)
def _enforce_citation_on_finding(mapper, connection, target: Finding) -> None:
    if not target.cited_provisions or len(target.cited_provisions) == 0:
        raise EmptyCitationError(
            "finding.cited_provisions cannot be empty — every finding must cite "
            "at least one provision (plan §9.1, citation enforcement)."
        )
    if not target.cited_documents or len(target.cited_documents) == 0:
        raise EmptyCitationError(
            "finding.cited_documents cannot be empty — every finding must cite "
            "the source documents of its provisions."
        )


@event.listens_for(Session, "before_flush")
def _enforce_citation_on_flush(session: Session, flush_context, instances) -> None:
    """Apply the same check at flush time, so Finding rows added via ORM merge are caught too."""
    for obj in session.new:
        if isinstance(obj, Finding):
            _enforce_citation_on_finding(None, None, obj)


async def audit_table_writeable_check(session: AsyncSession) -> bool:
    """Return True if the append-only trigger on audit_event is installed."""
    result = await session.execute(
        text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_audit_event_no_update'")
    )
    return result.scalar_one() == 1
