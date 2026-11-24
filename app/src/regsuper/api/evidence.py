"""Reproducible evidence pack for one analysis: a read-only, content-addressed bundle.

The pack embeds the canonical audit export (:mod:`regsuper.api.export`) unchanged and adds three
proofs: a temporal proof (each cited provision's validity window against the analysis date), a
refusal and grounding trail per finding, and the append-only audit trail. Rebuilding it from the
same immutable rows yields the same SHA-256, so any silent change is detectable. Off by default.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from regsuper.api.auth import get_principal

# Reuse the export's canonical hashing, content assembler and provision loader, so the embedded
# export is byte-identical to a standalone export and both share one hashing implementation.
from regsuper.api.export import (
    assemble_analysis_content,
    build_canonical_payload,
    resolve_cited_provisions,
)
from regsuper.config import get_settings
from regsuper.storage.database import session_scope
from regsuper.storage.models import AnalysisRun, AuditEvent, Finding, Project

router = APIRouter(prefix="/api", tags=["evidence"])

# Evidence-pack schema version. It is part of the hashed content, so a schema change changes the
# hash; reproducibility holds within one schema version (as with export.SCHEMA_VERSION).
EVIDENCE_SCHEMA_VERSION = "evidence-pack/1.0"


# ------------------------------------------------------- pure helpers (no DB, clock or randomness)
def _iso(value: date | datetime | None) -> str | None:
    """Return an ISO-8601 string for a date or datetime, or None.

    The inputs are immutable historical facts (provision windows, the analysis date, an audit row's
    timestamp), so the output is stable.
    """
    return value.isoformat() if value is not None else None


def _brackets_as_of(as_of: date | None, valid_from: date | None, valid_to: date | None) -> bool:
    """Return True if the half-open window ``[valid_from, valid_to)`` contains ``as_of``.

    Mirrors the candidate-retrieval mask ``(valid_from IS NULL OR valid_from <= as_of) AND
    (valid_to IS NULL OR valid_to > as_of)``: a NULL bound is open. With ``as_of`` None (a
    current-law analysis that makes no point-in-time claim) the result is True.
    """
    if as_of is None:
        return True
    if valid_from is not None and valid_from > as_of:
        return False
    if valid_to is not None and valid_to <= as_of:
        return False
    return True


def _temporal_proof(
    as_of: date | None, provisions: list[Any], cited_provision_ids: set[str]
) -> dict[str, Any]:
    """Build the point-in-time proof: each resolved cited provision's window against ``as_of``.

    Coverage is reported so the verdict can never be vacuously true:

    * ``cited_provision_ids`` is the full set of ids the findings cite. A cited id with no matching
      ``provision`` row (a dangling citation; ``cited_provisions`` is a plain ``UUID[]`` without a
      foreign key) is listed in ``unresolved_provision_ids`` and makes ``all_cited_resolved``
      False, so an analysis that is not fully grounded never reads as proven.
    * ``all_brackets`` covers the resolved provisions only and is trivially True on an empty set;
      read it together with ``n_resolved`` and ``all_cited_resolved``. The response's
      ``temporal_proof_ok`` also requires ``n_resolved >= 1``.

    ``as_of`` is the date of the project's latest ``AnalysisRun``, applied to all findings
    (``Finding`` has no link to a specific run). When it is None (a current-law analysis, which is
    currently every run, since nothing populates ``AnalysisRun.as_of`` yet) every window brackets
    trivially, and the proof only shows that the cited provisions resolve, not that they were in
    force on a given date. ``as_of`` is included so a reader can tell the two apart.
    """
    entries: list[dict[str, Any]] = []
    for p in provisions:
        vf = getattr(p, "valid_from", None)
        vt = getattr(p, "valid_to", None)
        entries.append(
            {
                "provision_id": str(getattr(p, "id", "")),
                "valid_from": _iso(vf),
                "valid_to": _iso(vt),
                "brackets_as_of": _brackets_as_of(as_of, vf, vt),
                "temporal_status": getattr(p, "temporal_status", None),
                "bindingness_tier": getattr(p, "bindingness_tier", None),
            }
        )
    entries.sort(key=lambda e: e["provision_id"])
    resolved_ids = {e["provision_id"] for e in entries}
    cited = {str(c) for c in cited_provision_ids}
    unresolved = sorted(cited - resolved_ids)
    return {
        "as_of": _iso(as_of),
        "n_cited": len(cited),
        "n_resolved": len(resolved_ids),
        "unresolved_provision_ids": unresolved,
        "all_cited_resolved": not unresolved,
        "provisions": entries,
        "all_brackets": all(e["brackets_as_of"] for e in entries),
    }


def _refusal_trail(findings: list[Any]) -> dict[str, Any]:
    """Build the refusal and grounding trail: one entry per finding, sorted by finding id.

    Each entry holds the finding's answer mode, compliance decision and citation counts.
    ``any_refusal`` is True if any finding is a refusal. ``all_findings_grounded`` is True if every
    finding cites at least one provision and one document (the citation rule the database CHECK
    constraint enforces).
    """
    entries: list[dict[str, Any]] = []
    for f in findings:
        n_prov = len(getattr(f, "cited_provisions", None) or [])
        n_doc = len(getattr(f, "cited_documents", None) or [])
        entries.append(
            {
                "finding_id": str(getattr(f, "id", "")),
                "finding_type": getattr(f, "finding_type", None),
                "answer_mode": getattr(f, "answer_mode", None),
                "compliance_reason": getattr(f, "compliance_reason", None),
                "compliance_decided_by": getattr(f, "compliance_decided_by", None),
                "disclaimer_id": getattr(f, "disclaimer_id", None),
                "n_cited_provisions": n_prov,
                "n_cited_documents": n_doc,
            }
        )
    entries.sort(key=lambda e: e["finding_id"])
    return {
        "n_findings": len(entries),
        "findings": entries,
        "any_refusal": any(e["finding_type"] == "refusal" for e in entries),
        # Trivially True for an empty finding set; reported next to n_findings so it is never
        # read alone.
        "all_findings_grounded": all(
            e["n_cited_provisions"] >= 1 and e["n_cited_documents"] >= 1 for e in entries
        ),
    }


def _audit_trail(events: list[Any]) -> list[dict[str, Any]]:
    """Render the subject's append-only ``audit_event`` rows as deterministic content blocks.

    This is the record-keeping trail required by EU AI Act Art. 12. ``occurred_at`` and the
    BIGSERIAL ``id`` never change on an append-only row, so including them is reproducible. Rows
    are sorted by ``(occurred_at, id)``: the database returns timestamps with a stable UTC offset,
    so the ISO strings sort chronologically, and the id breaks ties for a total order. GDPR erasure
    is recorded as a separate tombstone and never mutates these rows.
    """
    entries: list[dict[str, Any]] = []
    for e in events:
        entries.append(
            {
                "occurred_at": _iso(getattr(e, "occurred_at", None)),
                "actor_type": getattr(e, "actor_type", None),
                "action": getattr(e, "action", None),
                "target_type": getattr(e, "target_type", None),
                "target_id": getattr(e, "target_id", None),
                "audit_event_id": int(getattr(e, "id", 0) or 0),
            }
        )
    entries.sort(key=lambda x: (x["occurred_at"] or "", x["audit_event_id"]))
    return entries


def assemble_evidence_content(
    *,
    analysis_content: dict[str, Any],
    as_of: date | None,
    provisions: list[Any],
    findings: list[Any],
    audit_events: list[Any],
    cited_provision_ids: set[str],
) -> dict[str, Any]:
    """Build the hashed content of an evidence pack from already-fetched rows (pure; no I/O).

    Embeds the canonical analysis export unchanged (``analysis_export``) with its standalone hash
    (``analysis_export_hash``) for cross-checking, followed by the temporal, refusal and audit
    proofs. There is no pack id, generation timestamp or requester: such per-generation fields
    would break reproducibility, so they belong on the envelope, as with the audit export.
    """
    _, analysis_export_hash = build_canonical_payload(analysis_content)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "pack_type": "analysis-evidence",
        "subject_id": str(analysis_content.get("subject_id", "")),
        "as_of": _iso(as_of),
        "analysis_export": analysis_content,
        "analysis_export_hash": analysis_export_hash,
        "temporal_proof": _temporal_proof(as_of, provisions, cited_provision_ids),
        "refusal_trail": _refusal_trail(findings),
        "audit_trail": _audit_trail(audit_events),
    }


def build_evidence_pack(
    *,
    analysis_content: dict[str, Any],
    as_of: date | None,
    provisions: list[Any],
    findings: list[Any],
    audit_events: list[Any],
    cited_provision_ids: set[str],
) -> tuple[dict[str, Any], str]:
    """Return ``(content, content_hash)``: the evidence content and its canonical SHA-256.

    Pure: the same inputs always produce byte-identical content and the same hash, which is what
    makes the pack tamper-evident. Hashing reuses :func:`export.build_canonical_payload`.
    """
    content = assemble_evidence_content(
        analysis_content=analysis_content,
        as_of=as_of,
        provisions=provisions,
        findings=findings,
        audit_events=audit_events,
        cited_provision_ids=cited_provision_ids,
    )
    _, content_hash = build_canonical_payload(content)
    return content, content_hash


# --------------------------------------------------------------------------- response model
class EvidencePackResponse(BaseModel):
    """Evidence pack response: the subject, verdict flags, content hash and full content.

    The content contains no secrets: it is the same material the audit export exposes, plus the
    temporal, refusal and audit proofs.
    """

    subject_id: UUID
    as_of: date | None = None
    schema_version: str
    content_hash: str
    temporal_proof_ok: bool
    any_refusal: bool
    all_findings_grounded: bool
    content: dict[str, Any]


# --------------------------------------------------------------- route (feature-flagged, read-only)
def _require_enabled() -> None:
    """Raise 404 unless ``audit_evidence_enabled`` is set (it is off by default).

    A 404 rather than a 403, like the audit-export gate, so a disabled deployment does not reveal
    that the endpoint exists.
    """
    if not get_settings().audit_evidence_enabled:
        raise HTTPException(status_code=404, detail="not found")


@router.post("/evidence-pack/projects/{project_id}", response_model=EvidencePackResponse)
async def evidence_pack_for_project(project_id: UUID, request: Request) -> EvidencePackResponse:
    """Assemble a reproducible evidence pack for one analysis (project).

    Loads the project's findings, cited provisions, analysis date (from the latest run) and audit
    trail, and returns the deterministic content with its SHA-256. Rebuilding the pack from
    unchanged data returns the same hash. Read-only: nothing is written. Returns 404 for an
    unknown project, or when the evidence pack is disabled.
    """
    _require_enabled()
    await get_principal(request)  # authenticate (fail closed) and set the tenancy context
    settings = get_settings()
    async with session_scope() as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="project not found")

        findings = list(
            (await session.execute(select(Finding).where(Finding.project_id == project_id)))
            .scalars()
            .all()
        )
        # Keep the full set of cited ids so the temporal proof can report which ones did not
        # resolve (dangling citations). `resolve_cited_provisions` returns only ids that exist:
        # app-corpus ids from the provision table, engine-corpus ids via the engine. The proof
        # sorts internally, so the resolved list is passed unordered.
        cited_ids_raw: set[UUID] = set()
        for f in findings:
            cited_ids_raw.update(f.cited_provisions or [])
        provisions_by_id = await resolve_cited_provisions(session, findings)
        provisions = list(provisions_by_id.values())
        cited_provision_ids = {str(x) for x in cited_ids_raw}

        # The analysis date is that of the project's latest run (None means current law).
        as_of = (
            await session.execute(
                select(AnalysisRun.as_of)
                .where(AnalysisRun.project_id == project_id)
                .order_by(AnalysisRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        audit_events = list(
            (await session.execute(select(AuditEvent).where(AuditEvent.project_id == project_id)))
            .scalars()
            .all()
        )

        analysis_content = assemble_analysis_content(
            project, findings, provisions_by_id, settings=settings
        )
        content, content_hash = build_evidence_pack(
            analysis_content=analysis_content,
            as_of=as_of,
            provisions=provisions,
            findings=findings,
            audit_events=audit_events,
            cited_provision_ids=cited_provision_ids,
        )
        tp = content["temporal_proof"]
        rt = content["refusal_trail"]
        # The headline verdict is deliberately strict: True only when every cited provision
        # resolved, at least one did, and all were in force on the analysis date. An empty set, a
        # dangling citation or an out-of-window provision all read False.
        temporal_proof_ok = bool(
            tp["all_brackets"] and tp["all_cited_resolved"] and tp["n_resolved"] >= 1
        )
        return EvidencePackResponse(
            subject_id=project_id,
            as_of=as_of,
            schema_version=EVIDENCE_SCHEMA_VERSION,
            content_hash=content_hash,
            temporal_proof_ok=temporal_proof_ok,
            any_refusal=bool(rt["any_refusal"]),
            all_findings_grounded=bool(rt["all_findings_grounded"]),
            content=content,
        )
