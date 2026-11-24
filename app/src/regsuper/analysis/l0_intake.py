"""L0, project intake: the first stage of the analysis cascade.

A user describes their project; we deterministically map its ``sector`` and ``entity_type`` to
the internal tag vocabularies (``sector_tags``, ``activity_tags``) that every later layer uses. The
mapping is hardcoded by design: it must be reviewable by a lawyer and stable across versions,
whereas LLM-generated tags would invalidate the evaluation sets with every prompt change.

Anything we cannot map passes through as the raw user value, so unusual sectors are not silently
dropped; they rely on jurisdiction and free-text retrieval instead of the tag pre-filter.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import select

from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.storage.database import session_scope
from regsuper.storage.models import Project


# ---------------------------------------------------------------------------
# Intake schema
# ---------------------------------------------------------------------------
class ProjectIntake(BaseModel):
    """The project intake payload, mirroring the Next.js intake form."""

    name: str = Field(..., min_length=1, max_length=300)
    sector: str = Field(..., min_length=1, max_length=100)
    jurisdiction_scope: list[str] = Field(..., min_length=1, max_length=20)
    entity_type: str | None = Field(default=None, max_length=120)
    # Optional structured entity-role override (for example "aifm"). When unset the role is
    # derived from entity_type. Only read when entity_scoping_enabled is on.
    entity_role: str | None = Field(default=None, max_length=120)
    declared_intent: str | None = Field(default=None, max_length=4000)
    declared_risks: list[str] | None = Field(default=None, max_length=50)
    risk_tolerance: str = Field(default="moderate", max_length=40)
    analysis_depth: str = Field(default="standard", max_length=40)
    # Free-form data the frontend can pass through (location, contact info,
    # tenancy, etc.), persisted into project.metadata.
    extra: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Deterministic tag mappings
# ---------------------------------------------------------------------------
# Sector buckets that appear in the regulatory corpus. Each row expands
# to a wider set of provision-level tags so a user picking
# "fin_services" pulls in capital markets, AML, investment funds, and
# consumer credit content even if they didn't think to ask for them.
SECTOR_TO_TAGS: dict[str, list[str]] = {
    "fin_services": [
        "fin_services",
        "investment_funds",
        "capital_markets",
        "banking",
        "aml",
        "consumer_credit",
        "payments",
    ],
    "investment_funds": [
        "fin_services",
        "investment_funds",
        "capital_markets",
        "aml",
    ],
    "banking": ["fin_services", "banking", "aml", "consumer_credit", "payments"],
    "insurance": ["fin_services", "insurance", "consumer_protection"],
    "real_estate": ["real_estate", "leasing", "construction", "consumer_protection"],
    "energy": ["energy", "utilities", "environmental", "consumer_protection"],
    "tech": ["tech", "data_protection", "ai_act", "consumer_protection"],
    "healthcare": ["healthcare", "pharma", "medical_devices", "data_protection"],
    "manufacturing": ["manufacturing", "product_safety", "environmental"],
    "transport": ["transport", "logistics", "consumer_protection"],
    "retail": ["retail", "consumer_protection", "consumer_credit"],
    "crypto": ["fin_services", "crypto", "aml", "mica"],
    # Non-finance sectors. Each expands to the provision-level sector_tags the
    # corpus carries and also drives the engine corpus's legal_domain scope via
    # _SECTOR_TO_NEXUS_DOMAIN in l1_candidates. The first tag of each row maps
    # onto a populated engine domain; the rest widen the app-side sector_tags
    # overlap filter. Keep the leading tag stable: it anchors the legal domain.
    "civil": ["civil", "civil_obligations", "consumer_protection"],
    "civil_procedure": ["civil_procedure", "civil", "enforcement"],
    "administrative": ["administrative", "public_administration", "foia"],
    "labour": ["labour", "employment", "occupational_safety"],
    "social_security": ["social_security", "pension", "labour"],
    "ip": ["ip", "copyright", "civil"],
    "real_property": ["real_property", "civil", "construction", "cadastre"],
    "competition": ["competition", "procurement", "public_procurement"],
    "tax": ["tax", "accounting"],
    "criminal": ["criminal", "criminal_procedure"],
    "commercial": ["commercial", "corporate", "civil"],
    "construction": ["construction", "real_property", "environmental"],
}

# Entity types to activity tags. Activity tags capture *what* a regulated
# entity does, independent of which sector they sit in. An AIFM and a UCITS
# manager are both "fund_management" + "marketing"; a credit institution and
# a payment institution overlap on "aml".
ENTITY_TO_ACTIVITY: dict[str, list[str]] = {
    "aif": ["fund_management", "marketing", "passporting", "risk_management"],
    "aifm": ["fund_management", "marketing", "passporting", "risk_management"],
    "alternative_investment_fund": ["fund_management", "marketing", "passporting"],
    "investicní_fond": ["fund_management", "marketing", "passporting"],
    "investiční_fond": ["fund_management", "marketing", "passporting"],
    "investment_company": ["fund_management", "investment_services"],
    "ucits": ["fund_management", "marketing", "passporting"],
    "investment_firm": ["investment_services", "client_funds", "mifid", "best_execution"],
    "bank": ["lending", "deposit_taking", "aml", "capital_requirements"],
    "credit_institution": ["lending", "deposit_taking", "aml"],
    "payment_institution": ["payments", "aml", "data_protection"],
    "e_money": ["e_money", "payments", "aml"],
    "fintech": ["payments", "aml", "data_protection"],
    "crypto_provider": ["mica", "caspr", "aml", "data_protection"],
    "casp": ["mica", "caspr", "aml"],
    "insurance_undertaking": ["insurance", "solvency", "consumer_protection"],
    "broker": ["investment_services", "consumer_protection", "mifid"],
    # Entity types for the non-finance sectors. Activity tags never drive the
    # engine's domain scope (only sector_tags do); they widen the app-side
    # activity filter and keep unknown entities from being dropped.
    "employer": ["employment", "occupational_safety", "data_protection"],
    "public_authority": ["public_administration", "foia", "procurement"],
    "contracting_authority": ["public_procurement", "procurement", "transparency"],
    "data_controller": ["data_protection", "foia"],
    "landlord": ["real_property", "consumer_protection"],
    "developer": ["construction", "real_property", "environmental"],
    "ip_holder": ["copyright", "ip"],
    "taxpayer": ["tax", "accounting"],
}


def _norm(s: str | None) -> str:
    return (s or "").strip().lower().replace(" ", "_")


def derive_tags(intake: ProjectIntake) -> tuple[list[str], list[str]]:
    """Map an intake to ``(sector_tags, activity_tags)`` deterministically, without an LLM.

    Unknown sector or entity values pass through as the raw value (lower-cased,
    spaces as underscores) so the project still picks up something via
    jurisdiction even if the taxonomy does not cover the niche.
    """
    sector_key = _norm(intake.sector)
    sector_tags = SECTOR_TO_TAGS.get(sector_key, [sector_key]) if sector_key else []

    entity_key = _norm(intake.entity_type)
    activity_tags = list(ENTITY_TO_ACTIVITY.get(entity_key, []))
    if entity_key and entity_key not in {t.lower() for t in activity_tags}:
        # Don't drop unrecognised entity types; keep them as a tag so future
        # tagging passes on the corpus can still match.
        activity_tags.append(entity_key)

    # Dedup while preserving order.
    seen: set[str] = set()
    sector_tags = [t for t in sector_tags if not (t in seen or seen.add(t))]
    seen = set()
    activity_tags = [t for t in activity_tags if not (t in seen or seen.add(t))]
    return sector_tags, activity_tags


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
async def create_project(
    intake: ProjectIntake,
    *,
    client_id: UUID,
) -> Project:
    """Validate the intake, derive tags and persist a ``project`` row.

    Returns the persisted Project with ``id``, ``created_at`` and ``metadata_``
    populated. The derived tags go into ``metadata_['sector_tags']`` and
    ``metadata_['activity_tags']``: the Project model has no such columns, and
    keeping them in metadata keeps L1's filter logic self-contained without a
    schema migration.

    ``client_id`` is a legacy keyword name (the caller passes the org derived from
    the principal). Migration 006 renamed the column ``client_id`` to ``org_id`` in
    place, so the value is written to ``Project.org_id``; the keyword is kept so
    existing callers keep working.
    """
    sector_tags, activity_tags = derive_tags(intake)
    metadata = dict(intake.extra)
    metadata.update(
        {
            "sector_tags": sector_tags,
            "activity_tags": activity_tags,
            "intake_version": 1,
        }
    )
    # With entity_scoping_enabled on, derive and persist the structured entity role used by the
    # applicability pass after L3. With it off (the default) the key is never written.
    if get_settings().entity_scoping_enabled:
        from regsuper.analysis.applicability import project_entity_role

        metadata["entity_role"] = project_entity_role(
            intake.entity_role or intake.entity_type, intake.declared_intent
        )

    project = Project(
        org_id=client_id,  # migration 006 renamed the column client_id to org_id
        name=intake.name,
        sector=intake.sector,
        jurisdiction_scope=list(intake.jurisdiction_scope),
        entity_type=intake.entity_type,
        declared_intent=intake.declared_intent,
        declared_risks=list(intake.declared_risks or []),
        risk_tolerance=intake.risk_tolerance,
        analysis_depth=intake.analysis_depth,
        metadata_=metadata,
    )
    async with session_scope() as session:
        session.add(project)
        await session.flush()
        pid = project.id
    await emit(
        "project.created",
        project_id=str(pid),
        name=intake.name,
        sector=intake.sector,
        jurisdiction=intake.jurisdiction_scope,
    )
    return project


async def get_project(project_id: UUID) -> Project | None:
    async with session_scope() as session:
        return (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
