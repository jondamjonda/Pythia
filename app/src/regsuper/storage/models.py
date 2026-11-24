"""SQLAlchemy ORM models for the app database.

The schema itself is owned by the numbered raw-SQL files in ``migrations/``; these classes only
mirror it (``Base.metadata.create_all`` is never used). Section headers name the migration that
introduced each group of tables.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    cast,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from regsuper.corpus import NEXUS_CORPUS


class LTREE(UserDefinedType):
    """Minimal LTREE column type that casts text bind parameters to ltree on insert."""

    cache_ok = True

    def get_col_spec(self, **kw):
        return "LTREE"

    def bind_expression(self, bindvalue):
        return cast(bindvalue, LTREE())


class Geometry(UserDefinedType):
    """Opaque PostGIS geometry column type (geoalchemy2 is not a dependency).

    Mirrors the GEOMETRY(Point,4326) and GEOMETRY(Polygon,4326) columns from migration 001 so
    they appear on ``Project.__table__.columns``. The ORM layer needs no spatial operations;
    only the column's presence matters.
    """

    cache_ok = True

    def get_col_spec(self, **kw):
        return "geometry"


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# source_document
# ---------------------------------------------------------------------------
class SourceDocument(Base):
    __tablename__ = "source_document"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    external_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    publisher: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(Text, nullable=False)
    legal_hierarchy: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    raw_storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    s3_uri: Mapped[str | None] = mapped_column(Text)
    raw_format: Mapped[str | None] = mapped_column(Text)
    raw_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    superseded_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_document.id")
    )
    ingestion_pattern_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ingested")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


# ---------------------------------------------------------------------------
# provision
# ---------------------------------------------------------------------------
class Provision(Base):
    __tablename__ = "provision"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    source_document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("provision.id")
    )
    hierarchy_path: Mapped[str] = mapped_column(LTREE(), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    eid: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_format: Mapped[str] = mapped_column(Text, nullable=False, default="gfm")
    ingestion_quality: Mapped[float | None] = mapped_column(REAL)
    text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    legal_hierarchy: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction: Mapped[str] = mapped_column(Text, nullable=False)

    sector_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    entity_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    activity_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)

    deontic_modal: Mapped[str | None] = mapped_column(Text)
    deontic_actor: Mapped[str | None] = mapped_column(Text)
    deontic_action: Mapped[str | None] = mapped_column(Text)
    deontic_condition: Mapped[str | None] = mapped_column(Text)
    is_definition: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    defined_terms: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)

    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    embedding_semantic: Mapped[list[float] | None] = mapped_column(Vector(1536))
    embedding_domain: Mapped[list[float] | None] = mapped_column(Vector(1536))
    resolved_text: Mapped[str | None] = mapped_column(Text)
    embedding_resolved: Mapped[list[float] | None] = mapped_column(Vector(1536))

    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)

    # Database-generated full-text column from migration 001 (mirrored only, no DDL):
    # tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED.
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True)
    )

    # Trust and temporal columns (migration 013). The database CHECK constraints allow NULL or
    # one of the hyphenated values below; the ORM mirrors them as plain TEXT.
    #   bindingness_tier: 'hard-law', 'oop', 'soft-law', 'interpretive'
    #   temporal_status:  'in-force', 'future-effective', 'annulled', 'superseded'
    bindingness_tier: Mapped[str | None] = mapped_column(Text)
    temporal_status: Mapped[str | None] = mapped_column(Text)
    consolidated_informative: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_force_as_of_note: Mapped[str | None] = mapped_column(Text)

    # Anchor flag and point-in-time provenance (migration 014).
    is_anchor_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    valid_from_approximate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ingested_by: Mapped[str | None] = mapped_column(Text, default="polarius-ingest")


class ProvisionSentence(Base):
    __tablename__ = "provision_sentence"
    __table_args__ = (UniqueConstraint("provision_id", "sentence_index"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    provision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("provision.id", ondelete="CASCADE"),
        nullable=False,
    )
    sentence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="sentence")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))


# ---------------------------------------------------------------------------
# cross_reference
# ---------------------------------------------------------------------------
class CrossReference(Base):
    __tablename__ = "cross_reference"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    from_provision: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("provision.id", ondelete="CASCADE"),
        nullable=False,
    )
    to_provision: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("provision.id")
    )
    to_external_ref: Mapped[str | None] = mapped_column(Text)
    ref_type: Mapped[str] = mapped_column(Text, nullable=False)
    detected_by: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    verified_by: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


# ---------------------------------------------------------------------------
# defined_term
# ---------------------------------------------------------------------------
class DefinedTerm(Base):
    __tablename__ = "defined_term"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    term: Mapped[str] = mapped_column(Text, nullable=False)
    scope_document_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("source_document.id", ondelete="CASCADE")
    )
    definition_provision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("provision.id", ondelete="CASCADE"),
        nullable=False,
    )
    definition_text: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_by: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# project / project_document / project_context
# ---------------------------------------------------------------------------
class Project(Base):
    __tablename__ = "project"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    # Tenant column (NOT NULL): foreign key to organization with ON DELETE RESTRICT
    # (fk_project_org).
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organization.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Optional foreign key to business_profile, ON DELETE SET NULL (migration 010).
    business_profile_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("business_profile.id", ondelete="SET NULL"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sector: Mapped[str] = mapped_column(Text, nullable=False)
    jurisdiction_scope: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    # PostGIS columns from migration 001 (mirrored only, as the opaque Geometry type).
    location: Mapped[Any | None] = mapped_column(Geometry)
    location_polygon: Mapped[Any | None] = mapped_column(Geometry)
    declared_intent: Mapped[str | None] = mapped_column(Text)
    declared_risks: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    risk_tolerance: Mapped[str] = mapped_column(Text, nullable=False, default="moderate")
    analysis_depth: Mapped[str] = mapped_column(Text, nullable=False, default="standard")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class ProjectDocument(Base):
    __tablename__ = "project_document"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    raw_storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extracted_entities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProjectContext(Base):
    __tablename__ = "project_context"

    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        primary_key=True,
    )
    macro_summary: Mapped[str | None] = mapped_column(Text)
    applicable_provisions: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), default=list
    )
    meso_summaries: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    contradiction_tensor: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    analysis_state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")


# ---------------------------------------------------------------------------
# finding / hypothesis
# ---------------------------------------------------------------------------
class Finding(Base):
    __tablename__ = "finding"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    finding_type: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)

    cited_provisions: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )
    cited_documents: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False
    )

    confidence_overall: Mapped[float] = mapped_column(REAL, nullable=False)
    confidence_source_coverage: Mapped[float | None] = mapped_column(REAL)
    confidence_retrieval: Mapped[float | None] = mapped_column(REAL)
    confidence_interpretation: Mapped[float | None] = mapped_column(REAL)
    confidence_temporal: Mapped[float | None] = mapped_column(REAL)

    analysis_layer: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(Text)

    review_state: Mapped[str] = mapped_column(Text, nullable=False, default="unreviewed")
    reviewed_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_outcome: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("finding.id")
    )

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Tenant column, backfilled from the owning project. There is no database foreign key on
    # finding.org_id (column and index only), so it is mirrored as a bare UUID NOT NULL.
    org_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    # Answer mode and compliance-decision provenance (migration 012).
    answer_mode: Mapped[str] = mapped_column(Text, nullable=False, default="information")
    compliance_reason: Mapped[str | None] = mapped_column(Text)
    compliance_decided_by: Mapped[str | None] = mapped_column(Text)
    # Nullable TEXT reference to disclaimer.id, without a foreign key.
    disclaimer_id: Mapped[str | None] = mapped_column(Text)


class Hypothesis(Base):
    __tablename__ = "hypothesis"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    polarity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(REAL)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    derived_findings: Mapped[list[UUID]] = mapped_column(ARRAY(PG_UUID(as_uuid=True)), default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ---------------------------------------------------------------------------
# audit_event (append-only — DB trigger blocks UPDATE/DELETE)
# ---------------------------------------------------------------------------
class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("project.id"))
    model_version: Mapped[str | None] = mapped_column(Text)
    prompt_hash: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    # Tenant column, inherited from the project where there is one. Nullable by design: rows
    # without a project_id stay visible to platform admins only.
    org_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))


# ---------------------------------------------------------------------------
# retrieval_eval, retrieval_cache
# ---------------------------------------------------------------------------
class RetrievalEval(Base):
    __tablename__ = "retrieval_eval"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    expected_provision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("provision.id")
    )
    retrieved_provision_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("provision.id")
    )
    retrieval_score: Mapped[float] = mapped_column(REAL, nullable=False)
    rerank_score: Mapped[float | None] = mapped_column(REAL)
    label: Mapped[str | None] = mapped_column(Text)
    labeled_by: Mapped[str | None] = mapped_column(Text)
    labeled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Pooled-relevance labelling (migration 003).
    # All rows that judge the same query share `query_group_id`. `rank_position` is the 1-based
    # system rank when the row came from retrieval, and NULL for provisions the lawyer added by
    # hand. `label_kind` is the three-state verdict ('relevant', 'partial', 'not_relevant');
    # `label` is kept for the older binary threshold fitter.
    query_group_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    rank_position: Mapped[int | None] = mapped_column(Integer)
    added_by_lawyer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    label_kind: Mapped[str | None] = mapped_column(Text)


class RetrievalCache(Base):
    __tablename__ = "retrieval_cache"

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# ---------------------------------------------------------------------------
# api_call (platform observability — every external LLM/embedding request)
# ---------------------------------------------------------------------------
class ApiCall(Base):
    __tablename__ = "api_call"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # provider:sha256(secret)[:12], the budget line this call counts against. Nullable for older
    # rows, which migration 004 backfilled to legacy:<provider>.
    api_key_id: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("project.id", ondelete="SET NULL")
    )

    tokens_input: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # NUMERIC(12,6) in the database (widened by migration 011), mirrored exactly.
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)

    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text)

    request_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    response_summary: Mapped[str | None] = mapped_column(Text)

    # Tenant column; nullable because system calls without a project_id have no org.
    org_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    # Links the call's cost to its analysis run (ON DELETE SET NULL).
    analysis_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("analysis_run.id", ondelete="SET NULL")
    )


# ===========================================================================
# 006_tenancy: organization / "user" / membership (the tenancy core)
# ===========================================================================
class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    external_ref: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    org_type: Mapped[str] = mapped_column(Text, nullable=False, default="sme")  # 'sme'|'advisor'
    country: Mapped[str] = mapped_column(Text, nullable=False, default="CZ")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    referred_by: Mapped[str | None] = mapped_column(Text)  # referring partner, for attribution
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class User(Base):
    # DB table is the quoted reserved word "user"; SQLAlchemy quotes it automatically.
    __tablename__ = "user"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    password_hash: Mapped[str | None] = mapped_column(Text)  # Argon2id; nullable for SSO
    display_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (UniqueConstraint("user_id", "org_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        Text, nullable=False, default="member"
    )  # owner|admin|member|advisor
    seat_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="active"
    )  # active|invited|revoked
    invited_by: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("user.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ===========================================================================
# 013_trust_audit_export: session (mapped as UserSession)
# ===========================================================================
class UserSession(Base):
    # The table is "session"; the class is named UserSession to avoid clashing with
    # sqlalchemy.orm.Session, which storage/audit.py imports as `Session`.
    __tablename__ = "session"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )  # sha256 of opaque token
    active_org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)


# ===========================================================================
# 009_plans_entitlements: plan / entitlement / subscription
# ===========================================================================
class Plan(Base):
    __tablename__ = "plan"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )  # 'lite'|'business'|'advisor'
    name: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1|2|3
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="CZK")
    billing_period: Mapped[str] = mapped_column(Text, nullable=False, default="month")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Entitlement(Base):
    # Denormalised one-to-one with plan: the primary key is plan_id.
    __tablename__ = "entitlement"

    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plan.id", ondelete="CASCADE"), primary_key=True
    )
    max_depth: Mapped[str] = mapped_column(Text, nullable=False, default="L2")  # L2|L3|L4
    answer_mode: Mapped[str] = mapped_column(
        Text, nullable=False, default="information"
    )  # information|compliance
    queries_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10
    )  # -1 = unlimited
    analyses_per_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3
    )  # -1 = unlimited
    max_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    audit_export: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    api_access: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # Conduit reads this
    multi_client: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    change_alerts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Access to POST /api/answer. Granted on every tier (tiers differ in depth and quota, not in
    # whether you may ask), so the default is TRUE and require_entitlement('answer') passes.
    # Added by migration 009 on a fresh database and 020 on an existing one. Setting it FALSE
    # on a row withdraws answer access for that plan.
    answer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Subscription(Base):
    __tablename__ = "subscription"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("plan.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active"
    )  # trialing|active|past_due|canceled
    seats_purchased: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # NULL = current
    external_billing_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ===========================================================================
# 021_municipality_gazetteer: official Czech municipality gazetteer
# (obec, okres, ORP, kraj). Public reference data: no org_id and no RLS.
# ===========================================================================
class Municipality(Base):
    __tablename__ = "municipality"

    code: Mapped[str] = mapped_column(Text, primary_key=True)  # RÚIAN/ČSÚ 6-digit kód obce
    name: Mapped[str] = mapped_column(Text, nullable=False)
    okres_code: Mapped[str | None] = mapped_column(Text)  # LAU1 / CZ-NUTS4 (e.g. CZ0323)
    okres_name: Mapped[str | None] = mapped_column(Text)
    orp_code: Mapped[str | None] = mapped_column(Text)  # ČSÚ SO ORP code
    orp_name: Mapped[str | None] = mapped_column(Text)
    kraj_code: Mapped[str | None] = mapped_column(Text)  # NUTS3 (e.g. CZ032)
    kraj_name: Mapped[str | None] = mapped_column(Text)
    okres_csu_code: Mapped[str | None] = mapped_column(Text)  # ČSÚ internal 101 code (provenance)
    kraj_csu_code: Mapped[str | None] = mapped_column(Text)  # ČSÚ internal 100 code (provenance)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ===========================================================================
# 010_profiles_advisor: business_profile / advisor / advisor_client
# ===========================================================================
class BusinessProfile(Base):
    __tablename__ = "business_profile"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE")
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    legal_name: Mapped[str | None] = mapped_column(Text)
    ico: Mapped[str | None] = mapped_column(Text)  # IČO 8-digit string
    legal_form: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # sro|as|osvč|ks|vos|se|družstvo|other
    nace_code: Mapped[str | None] = mapped_column(Text)
    nace_description: Mapped[str | None] = mapped_column(Text)
    sector_override: Mapped[str | None] = mapped_column(Text)
    additional_activities: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    headcount_band: Mapped[str] = mapped_column(
        Text, nullable=False, default="2-9"
    )  # 1|2-9|10-49|50-249|250+
    annual_turnover_czk: Mapped[int | None] = mapped_column(BigInteger)
    # The five compliance gates; they drive tag derivation in the L0 intake step.
    processes_personal_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    handles_client_money: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_real_estate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cross_border: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_employer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    regulated_activity: Mapped[str | None] = mapped_column(Text)
    primary_jurisdiction: Mapped[str] = mapped_column(
        Text, nullable=False, default="CZ"
    )  # CZ|EU|CZ+EU
    registered_municipality: Mapped[str | None] = mapped_column(
        Text
    )  # free-text (legacy, back-compat)
    # Validated 6-digit RÚIAN/ČSÚ municipality code (foreign key to municipality.code).
    municipality_code: Mapped[str | None] = mapped_column(Text, ForeignKey("municipality.code"))
    preferred_language: Mapped[str] = mapped_column(Text, nullable=False, default="cs")  # cs|en
    analysis_depth: Mapped[str] = mapped_column(Text, nullable=False, default="standard")
    # Derived tags, written only by the L0 intake step, never by the UI or the intake form.
    derived_sector_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    derived_activity_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    tags_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Advisor(Base):
    __tablename__ = "advisor"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE")
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(
        Text, nullable=False, default="accountant"
    )  # accountant|lawyer|admin
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class AdvisorClient(Base):
    __tablename__ = "advisor_client"
    __table_args__ = (UniqueConstraint("advisor_id", "business_profile_id"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    advisor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("advisor.id", ondelete="CASCADE"), nullable=False
    )
    business_profile_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("business_profile.id", ondelete="CASCADE"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    granted_by: Mapped[str | None] = mapped_column(Text)
    access_level: Mapped[str] = mapped_column(
        Text, nullable=False, default="read"
    )  # read|edit|full


# ===========================================================================
# 011_runs_metering_billing: analysis_run / usage_counter / billing_event
# ===========================================================================
class AnalysisRun(Base):
    # One table for both analysis runs and background analysis jobs.
    # BIGSERIAL primary key: an integer surrogate, not a UUID.
    __tablename__ = "analysis_run"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id"), nullable=False
    )
    project_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("project.id", ondelete="SET NULL")
    )
    business_profile_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("business_profile.id", ondelete="SET NULL")
    )
    tier: Mapped[str | None] = mapped_column(Text)  # plan_code snapshot at run time
    requested_depth: Mapped[str | None] = mapped_column(Text)  # CHECK L2|L3|L4 at DB layer
    requested_layers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    executed_layers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="queued"
    )  # queued|running|done|failed|refused
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)
    tokens_input: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tokens_output: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    answer_mode: Mapped[str] = mapped_column(Text, nullable=False, default="information")
    worker_id: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[date | None] = mapped_column(Date)  # reserved for as-of-date runs; unused
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageCounter(Base):
    # Composite primary key (org_id, metric, period_kind, period_start); no surrogate id.
    __tablename__ = "usage_counter"

    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id"), primary_key=True
    )
    metric: Mapped[str] = mapped_column(
        Text, primary_key=True
    )  # queries|analyses|analyses_L4|api_calls
    period_kind: Mapped[str] = mapped_column(Text, primary_key=True)  # day|month
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)  # UTC bucket start
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, default=0)


class BillingEvent(Base):
    # The single append-only billing table (a database trigger blocks UPDATE and DELETE).
    # BIGSERIAL primary key; event_type is enforced by a CHECK constraint (including
    # 'conduit.call').
    __tablename__ = "billing_event"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # analysis.metered|quota.exceeded|seat.added|seat.removed|subscription.changed|conduit.call
    analysis_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("analysis_run.id"))
    conduit_call_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )  # Conduit attribution; FK-less
    amount_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    currency: Mapped[str | None] = mapped_column(Text)  # EUR advisor seats; CZK subs
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ===========================================================================
# 012_compliance: handoff / disclaimer
# ===========================================================================
class Handoff(Base):
    __tablename__ = "handoff"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("project.id", ondelete="SET NULL")
    )
    org_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id", ondelete="SET NULL")
    )
    trigger: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # reserved_advice|below_confidence|user_requested
    answer_mode: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_query: Mapped[str | None] = mapped_column(Text)
    cited_provisions: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="offered"
    )  # offered->accepted->sent->closed|declined
    partner_id: Mapped[str | None] = mapped_column(Text)
    partner_ref: Mapped[str | None] = mapped_column(Text)
    contact_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict
    )  # personal data only once consent_at is set
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Disclaimer(Base):
    # Public reference data (no RLS). The TEXT primary key is the stable id.
    __tablename__ = "disclaimer"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # e.g. 'info.v1'
    answer_mode: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # information|reserved_advice|needs_review
    confidence: Mapped[str] = mapped_column(Text, nullable=False, default="normal")  # normal|low
    text_cs: Mapped[str] = mapped_column(Text, nullable=False)
    text_en: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ===========================================================================
# 013_trust_audit_export: audit_export
# ===========================================================================
class AuditExport(Base):
    # Content is immutable: a database trigger allows UPDATE (to advance status) but blocks
    # DELETE.
    __tablename__ = "audit_export"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    export_type: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # CHECK analysis|finding at DB layer
    subject_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )  # project_id OR finding_id
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id"), nullable=False
    )
    requested_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )  # "user".id; FK-less
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending"
    )  # CHECK pending|generating|done|error
    schema_version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0")
    json_s3_uri: Mapped[str | None] = mapped_column(Text)
    pdf_s3_uri: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    payload_hash: Mapped[str | None] = mapped_column(Text)  # SHA-256 of canonical JSON
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


# ===========================================================================
# 014_corpus_cutover: provision_id_map (public, no RLS; created empty)
# ===========================================================================
class ProvisionIdMap(Base):
    __tablename__ = "provision_id_map"
    __table_args__ = (UniqueConstraint("nexus_id", "nexus_corpus"),)

    regsuper_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("provision.id", ondelete="CASCADE"), primary_key=True
    )
    nexus_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    nexus_corpus: Mapped[str] = mapped_column(Text, nullable=False, default=NEXUS_CORPUS)
    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


# ===========================================================================
# 016_conduit_credential: a staged migration, not part of the applied chain.
# The class is mapped so Conduit code can reference it; the table exists only once migration
# 016 is applied by hand, after row-level security is verified. Mapping a class for a table
# that may not exist is safe because Base.metadata.create_all is never used here (raw-SQL
# migrations own the DDL), so the table is never created by accident.
# ===========================================================================
class ConduitCredential(Base):
    __tablename__ = "conduit_credential"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organization.id", ondelete="CASCADE"), nullable=False
    )
    credential_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )  # 'conduit:<sha256(secret)[:12]>'
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="api_key"
    )  # api_key|oauth_client
    label: Mapped[str | None] = mapped_column(Text)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")  # active|revoked
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True)
    )  # FK-less to "user"; audit only
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
