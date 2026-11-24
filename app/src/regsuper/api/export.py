"""Reproducible audit export (EU AI Act record-keeping): a content-addressed analysis snapshot.

:func:`build_canonical_payload` turns an analysis (or one finding) into canonical JSON plus a
SHA-256, so re-exporting unchanged content yields byte-identical JSON and the same hash. Per-export
fields (id, timestamps, requester, file locations) live on the `audit_export` row, never in the
hashed content. Routes return 404 unless `audit_export_enabled` is set; PDF output is not built yet.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from regsuper.analysis.provision_view import NexusProvisionView
from regsuper.api.auth import get_principal
from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.pythia import client as pythia_client
from regsuper.storage.audit import log_event
from regsuper.storage.database import session_scope
from regsuper.storage.models import AuditExport, Finding, Project, Provision

# Marker the cascade stores in finding.payload['law_source'] when a finding was produced against
# the engine (Nexus) corpus; when absent, the finding cites the app corpus. Cited-provision
# resolution routes on this per-finding marker.
NEXUS_LAW_SOURCE = "nexus"

router = APIRouter(prefix="/api", tags=["export"])

# Canonical envelope version. It is part of the hashed content, so a schema change changes the
# hash; reproducibility holds within one schema version.
SCHEMA_VERSION = "1.0"

# Where the canonical JSON is written (a local directory instead of S3); recorded in json_s3_uri.
_EXPORT_DIR = Path("./data/exports")

# Length of the text excerpt per cited provision: enough for a reader or auditor to recognise
# the provision without copying the whole act. A fixed prefix, so it is deterministic.
_EXCERPT_CHARS = 600


# ---------------------------------------------------------------------------
# Pure core: the reproducible canonical-JSON and SHA-256 builder (no DB, clock or randomness)
# ---------------------------------------------------------------------------
def build_canonical_payload(content: dict[str, Any]) -> tuple[str, str]:
    """Canonicalise ``content`` and return ``(canonical_json, sha256_hex)``.

    Pure: the same ``content`` always yields the same bytes and the same hash. The canonical
    form is ``json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))``,
    encoded as UTF-8 and hashed with SHA-256. ``sort_keys`` makes the result independent of
    key order.

    Pass content only (see the ``assemble_*`` functions): no export id, ``requested_at``,
    ``generated_at``, ``requested_by``, ``json_s3_uri`` or ``pdf_s3_uri``. Such per-export
    fields would break reproducibility, so they live on the database row.
    """
    canonical_json = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    sha256_hex = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return canonical_json, sha256_hex


def _provision_excerpt(provision: Provision) -> str:
    """Return a deterministic, bounded text excerpt of a provision."""
    text = getattr(provision, "text", None) or ""
    return text[:_EXCERPT_CHARS]


def _citation_string(provision: Provision) -> str:
    """Return a deterministic, human-readable citation string for a provision.

    Follows the digest's ``render._legal_source`` convention so the export and the on-screen
    citation read the same: the ``label`` if present, else the structural ``eid``, the LTREE
    ``hierarchy_path`` or the bare id, plus a ``(<jurisdiction>/<legal_hierarchy>)`` qualifier
    when available.
    """
    label = getattr(provision, "label", None)
    eid = getattr(provision, "eid", None)
    hierarchy_path = getattr(provision, "hierarchy_path", None)
    head = label or eid or hierarchy_path
    if not head:
        head = str(getattr(provision, "id", ""))
    jurisdiction = getattr(provision, "jurisdiction", None)
    legal_hierarchy = getattr(provision, "legal_hierarchy", None)
    qualifier = "/".join(part for part in (jurisdiction, legal_hierarchy) if part)
    return f"{head} ({qualifier})" if qualifier else str(head)


def _provision_block(provision: Provision) -> dict[str, Any]:
    """Build the deterministic content block for one cited provision."""
    return {
        "provision_id": str(getattr(provision, "id", "")),
        "label": getattr(provision, "label", None),
        "citation": _citation_string(provision),
        "excerpt": _provision_excerpt(provision),
        # Trust fields (added by migration 013_trust_audit_export): temporal status and bindingness.
        "bindingness_tier": getattr(provision, "bindingness_tier", None),
        "temporal_status": getattr(provision, "temporal_status", None),
    }


def _finding_block(finding: Finding, provisions_by_id: dict[str, Provision]) -> dict[str, Any]:
    """Build the deterministic content block for one finding and its cited provisions.

    ``provisions_by_id`` maps a stringified provision id to the fetched ``Provision`` row. Only
    ids present in the map are emitted, sorted by id so the block does not depend on order.
    """
    cited_ids = [str(pid) for pid in (getattr(finding, "cited_provisions", None) or [])]
    blocks = [
        _provision_block(provisions_by_id[pid]) for pid in cited_ids if pid in provisions_by_id
    ]
    blocks.sort(key=lambda b: b["provision_id"])
    content: dict[str, Any] = {
        "finding_type": getattr(finding, "finding_type", None),
        "statement": getattr(finding, "statement", None),
        "severity": getattr(finding, "severity", None),
        "confidence_overall": getattr(finding, "confidence_overall", None),
        "review_state": getattr(finding, "review_state", None),
        "answer_mode": getattr(finding, "answer_mode", None),
        "provisions": blocks,
    }
    # Include the entity/role applicability verdict only when the scoring pass stored a
    # well-formed (dict) one, so findings without it keep their existing ``payload_hash``.
    # ``binds_confidence`` is dropped: it is the only float (an internal detail already reflected
    # in ``confidence_overall``), and leaving floats out keeps the hash immune to float-formatting
    # differences across platforms. The remaining fields are categorical.
    applicability = (getattr(finding, "payload", None) or {}).get("applicability")
    if isinstance(applicability, dict):
        content["applicability"] = {
            k: v for k, v in applicability.items() if k != "binds_confidence"
        }
    return content


def _ai_system_block(settings: Any) -> dict[str, Any]:
    """Build the AI-system identity block: model and law source.

    There is no ``corpus_as_of`` key, because no point-in-time corpus setting exists; the key is
    left out on every export rather than filled from a clock.
    """
    model = _resolve_model(settings)
    return {
        "product": "Polarius",
        "model": model,
        "law_source": getattr(settings, "law_source", None),
    }


def _resolve_model(settings: Any) -> str | None:
    """Return the drafting model's name (the OBLIGATIONS role) for the export envelope.

    Returns None rather than guessing when the resolver is unavailable. The resolver reads the
    environment once and caches it, so this is deterministic.
    """
    try:
        from regsuper.analysis.model_resolver import ModelRole, resolve_model

        return resolve_model(ModelRole.OBLIGATIONS)
    except Exception:
        return getattr(settings, "embedding_model", None)


def _data_residency_block(settings: Any) -> dict[str, Any]:
    """Build the data-residency and DPA block (EU AI Act Art. 13 transparency)."""
    return {
        "compute_region": getattr(settings, "data_residency_compute_region", None),
        "storage_region": getattr(settings, "data_residency_storage_region", None),
        "model_inference_provider": _inference_provider(settings),
        "training_on_user_data": False,
        "dpa_reference": getattr(settings, "openai_dpa_url", None),
    }


def _inference_provider(settings: Any) -> str:
    """Derive the inference provider's name from the resolved model."""
    model = (_resolve_model(settings) or "").lower()
    if model.startswith("claude") or "anthropic" in model:
        return "Anthropic"
    if model.startswith(("gpt", "o1", "o3", "text-embedding")):
        return "OpenAI"
    return "OpenAI"


def assemble_analysis_content(
    project: Project,
    findings: list[Finding],
    provisions_by_id: dict[str, Provision],
    *,
    settings: Any,
) -> dict[str, Any]:
    """Build the hashed content of an analysis export from already-fetched ORM rows.

    Pure: nothing is fetched here. Findings are sorted by id so the content does not depend on
    order, and there is no export id, timestamp, requester or location in it (see
    :func:`build_canonical_payload`).
    """
    finding_blocks = [_finding_block(f, provisions_by_id) for f in findings]
    finding_blocks.sort(key=lambda b: (b.get("statement") or "", b.get("finding_type") or ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "export_type": "analysis",
        "subject_id": str(getattr(project, "id", "")),
        "ai_system": _ai_system_block(settings),
        "query": {
            "sector": getattr(project, "sector", None),
            "jurisdiction": getattr(project, "jurisdiction_scope", None),
            "entity": getattr(project, "entity_type", None),
            "declared_intent": getattr(project, "declared_intent", None),
        },
        "answer_mode": _dominant_answer_mode(findings),
        "findings": finding_blocks,
        "data_residency": _data_residency_block(settings),
    }


def assemble_finding_content(
    finding: Finding,
    provisions_by_id: dict[str, Provision],
    *,
    settings: Any,
) -> dict[str, Any]:
    """Build the hashed content of a single-finding export from already-fetched ORM rows."""
    block = _finding_block(finding, provisions_by_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "export_type": "finding",
        "subject_id": str(getattr(finding, "id", "")),
        "ai_system": _ai_system_block(settings),
        "query": {
            "finding_type": getattr(finding, "finding_type", None),
            "statement": getattr(finding, "statement", None),
            "severity": getattr(finding, "severity", None),
            "project_id": str(getattr(finding, "project_id", "")),
        },
        "answer_mode": getattr(finding, "answer_mode", None),
        "findings": [block],
        "data_residency": _data_residency_block(settings),
    }


def _dominant_answer_mode(findings: list[Finding]) -> str:
    """Return one deterministic answer mode for the analysis envelope.

    'reserved_advice' if any finding is reserved advice, else 'needs_review' if any needs review,
    else 'information'. Independent of finding order.
    """
    modes = {getattr(f, "answer_mode", None) for f in findings}
    if "reserved_advice" in modes:
        return "reserved_advice"
    if "needs_review" in modes:
        return "needs_review"
    return "information"


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------
class ExportResponse(BaseModel):
    """Export response: the row's identity, the content hash and the artifact locations.

    Never includes the canonical JSON content or any secret.
    """

    id: UUID
    export_type: str
    subject_id: UUID
    status: str
    schema_version: str
    payload_hash: str | None = None
    json_s3_uri: str | None = None
    pdf_s3_uri: str | None = None
    error_detail: str | None = None


def _to_response(row: AuditExport) -> ExportResponse:
    """Map a row to the response model (without content or secrets)."""
    return ExportResponse(
        id=row.id,
        export_type=row.export_type,
        subject_id=row.subject_id,
        status=row.status,
        schema_version=row.schema_version,
        payload_hash=row.payload_hash,
        json_s3_uri=row.json_s3_uri,
        pdf_s3_uri=row.pdf_s3_uri,
        error_detail=row.error_detail,
    )


# ---------------------------------------------------------------------------
# Database helpers; the caller owns the transaction (as with handoff.create_handoff_offer)
# ---------------------------------------------------------------------------
async def create_audit_export(
    session: AsyncSession,
    *,
    export_type: str,
    subject_id: UUID,
    org_id: UUID | None,
    requested_by: UUID | None,
) -> AuditExport:
    """Create an audit_export row with ``status='pending'``.

    Writes an ``audit_export.created`` audit event and emits ``export.requested`` in the same
    session; the caller commits. Does not build the payload: that is :func:`generate_export`,
    mirroring the row's pending-to-done lifecycle. ``org_id`` is stamped so the row is
    tenant-attributed under row-level security. A database trigger allows status updates but
    blocks deletes.
    """
    if export_type not in ("analysis", "finding"):
        raise ValueError(f"invalid export_type {export_type!r}; expected 'analysis' or 'finding'")
    row = AuditExport(
        export_type=export_type,
        subject_id=subject_id,
        org_id=org_id,
        requested_by=requested_by,
        status="pending",
        schema_version=SCHEMA_VERSION,
    )
    session.add(row)
    await session.flush()  # populate row.id for the audit row and the response

    await log_event(
        session,
        actor_type="user",
        action="audit_export.created",
        target_type="audit_export",
        target_id=str(row.id),
        org_id=org_id,
        payload={"export_type": export_type, "subject_id": str(subject_id)},
    )
    await emit(
        "export.requested",
        module="polarius",
        kind=export_type,
        target_id=str(subject_id),
        export_id=str(row.id),
    )
    return row


async def _load_provisions(session: AsyncSession, provision_ids: set[UUID]) -> dict[str, Provision]:
    """Fetch the cited app-corpus provisions, keyed by stringified id."""
    if not provision_ids:
        return {}
    rows = (
        (await session.execute(select(Provision).where(Provision.id.in_(list(provision_ids)))))
        .scalars()
        .all()
    )
    return {str(p.id): p for p in rows}


def _finding_is_nexus(finding: Any) -> bool:
    """Return True if the finding cites engine-corpus (Nexus) ids rather than app provisions.

    Such ids resolve through the live engine, not the app `provision` table. The marker is stored
    in payload['law_source'] when the finding is persisted; when absent, the finding is app-corpus.
    """
    payload = getattr(finding, "payload", None) or {}
    return payload.get("law_source") == NEXUS_LAW_SOURCE


async def resolve_cited_provisions(session: AsyncSession, findings: list[Any]) -> dict[str, Any]:
    """Resolve every finding's cited provisions from the right corpus, keyed by ``str(id)``.

    Shared by the audit export and the evidence pack. App-corpus findings resolve against the app
    `provision` table (ORM `Provision` rows); engine-corpus findings resolve against the live engine
    (a `ProvisionView` with the same attributes). The two id spaces are disjoint UUIDs, so
    partitioning by the per-finding marker is unambiguous. An id the engine does not return,
    including when the engine is unreachable (a transport error counts as a miss and is not
    raised), is simply absent from the map. It then shows up downstream as an unresolved citation
    (the export omits its provision block; the evidence pack lists it in
    ``unresolved_provision_ids``), never substituted and never a 500.

    Known limitation: engine reads are live, and the engine corpus can change between two exports,
    so for engine-corpus findings the byte-identical re-export guarantee does not hold (and the
    embedded text is the current version, not the one at the analysis date). Reads must be pinned
    to a point-in-time engine snapshot before exports are enabled for the engine corpus (see
    NexusProvisionView.from_engine_dict).
    """
    app_ids: set[UUID] = set()
    nexus_ids: set[UUID] = set()
    for f in findings:
        (nexus_ids if _finding_is_nexus(f) else app_ids).update(f.cited_provisions or [])

    resolved: dict[str, Any] = await _load_provisions(session, app_ids)

    if nexus_ids:
        try:
            rows = await pythia_client.provisions([str(pid) for pid in nexus_ids])
        except Exception:
            rows = []  # engine unreachable: the ids stay unresolved rather than causing a 500
        by_engine_id = {str(r.get("id")): r for r in rows}
        for pid in nexus_ids:
            row = by_engine_id.get(str(pid))
            if row is not None:
                resolved[str(pid)] = NexusProvisionView.from_engine_dict(str(pid), row)
    return resolved


async def generate_export(session: AsyncSession, export_row: AuditExport) -> AuditExport:
    """Build the canonical payload for ``export_row`` and finalise the row.

    Loads the subject's project, findings and provisions, builds the content, computes
    ``(canonical_json, payload_hash)`` with the pure core and writes the JSON to
    ``./data/exports/{id}.json`` (a local stand-in for S3). Then sets ``payload_hash``,
    ``schema_version``, ``generated_at``, ``status='done'`` and ``json_s3_uri``, and emits
    ``export.ready``. ``pdf_s3_uri`` stays None because no PDF renderer is installed.

    Never raises for a content error: on any failure the row gets ``status='error'`` with a
    bounded ``error_detail`` and an ``export.refused`` event is emitted, so a failed export is a
    visible row rather than an exception in the caller's transaction. The caller commits.
    """
    settings = get_settings()
    try:
        if export_row.export_type == "analysis":
            project = await session.get(Project, export_row.subject_id)
            if project is None:
                raise LookupError(f"project {export_row.subject_id} not found")
            findings = (
                (await session.execute(select(Finding).where(Finding.project_id == project.id)))
                .scalars()
                .all()
            )
            provisions_by_id = await resolve_cited_provisions(session, list(findings))
            content = assemble_analysis_content(
                project, list(findings), provisions_by_id, settings=settings
            )
        elif export_row.export_type == "finding":
            finding = await session.get(Finding, export_row.subject_id)
            if finding is None:
                raise LookupError(f"finding {export_row.subject_id} not found")
            provisions_by_id = await resolve_cited_provisions(session, [finding])
            content = assemble_finding_content(finding, provisions_by_id, settings=settings)
        else:  # pragma: no cover - guarded at create
            raise ValueError(f"invalid export_type {export_row.export_type!r}")

        canonical_json, payload_hash = build_canonical_payload(content)

        # Write the canonical bytes to a local file (stand-in for S3).
        _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        json_path = _EXPORT_DIR / f"{export_row.id}.json"
        json_path.write_text(canonical_json, encoding="utf-8")

        export_row.payload_hash = payload_hash
        export_row.schema_version = SCHEMA_VERSION
        export_row.json_s3_uri = str(json_path)
        export_row.pdf_s3_uri = None  # no PDF renderer installed yet
        export_row.generated_at = datetime.now(UTC)
        export_row.status = "done"

        await log_event(
            session,
            actor_type="system",
            action="audit_export.generated",
            target_type="audit_export",
            target_id=str(export_row.id),
            org_id=export_row.org_id,
            payload={"payload_hash": payload_hash, "export_type": export_row.export_type},
        )
        await emit(
            "export.ready",
            module="polarius",
            export_id=str(export_row.id),
        )
    except Exception as exc:  # never raise; record the failure on the row
        export_row.status = "error"
        export_row.error_detail = str(exc)[:2000]
        await emit(
            "export.refused",
            module="polarius",
            level="warn",
            export_id=str(export_row.id),
            reason=str(exc)[:200],
        )
    return export_row


# ---------------------------------------------------------------------------
# Routes (all return 404 unless audit_export_enabled is set)
# ---------------------------------------------------------------------------
def _require_enabled() -> None:
    """Raise 404 unless ``audit_export_enabled`` is set (it is off by default).

    A 404 rather than a 403, so a disabled route is indistinguishable from a non-existent one.
    """
    if not get_settings().audit_export_enabled:
        raise HTTPException(status_code=404, detail="not found")


@router.post("/projects/{project_id}/export", response_model=ExportResponse)
async def export_project(project_id: UUID, request: Request) -> ExportResponse:
    """Create and generate an audit export of a project's analysis.

    Runs synchronously. A failed generation still returns the export with `status: "error"`.
    """
    _require_enabled()
    p = await get_principal(request)
    async with session_scope() as session:
        row = await create_audit_export(
            session,
            export_type="analysis",
            subject_id=project_id,
            org_id=p.org_id,
            requested_by=p.user_id,
        )
        await generate_export(session, row)
        return _to_response(row)


@router.post(
    "/projects/{project_id}/findings/{finding_id}/export",
    response_model=ExportResponse,
)
async def export_finding(project_id: UUID, finding_id: UUID, request: Request) -> ExportResponse:
    """Create and generate an audit export of a single finding.

    Runs synchronously. A failed generation still returns the export with `status: "error"`.
    """
    _require_enabled()
    p = await get_principal(request)
    async with session_scope() as session:
        row = await create_audit_export(
            session,
            export_type="finding",
            subject_id=finding_id,
            org_id=p.org_id,
            requested_by=p.user_id,
        )
        await generate_export(session, row)
        return _to_response(row)


@router.get("/exports/{export_id}", response_model=ExportResponse)
async def get_export(export_id: UUID, request: Request) -> ExportResponse:
    """Return one audit export. Returns 404 if it does not exist."""
    _require_enabled()
    await get_principal(request)
    async with session_scope() as session:
        row = await session.get(AuditExport, export_id)
        if row is None:
            raise HTTPException(status_code=404, detail="export not found")
        return _to_response(row)
