-- 001_initial_schema.sql
-- Regulatory Super App — universal data spine.
-- Single migration creates every table the system will ever use, even those
-- left empty until Advanced MVP. Schema is locked across stages (Section 2 of plan).
--
-- Markdown amendment (amendments/01-markdown-ingestion.md): provision.text is
-- documented as GFM Markdown. Two extra columns track format and ingestion quality.

BEGIN;

-- Extensions. PostGIS enabled at MVP per plan section 2.3 even though no
-- geospatial rows are loaded yet — enabling later requires a downtime window.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";


-- ============================================================================
-- source_document
-- ============================================================================
CREATE TABLE source_document (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    external_id          TEXT,
    title                TEXT NOT NULL,
    publisher            TEXT NOT NULL,
    jurisdiction         TEXT NOT NULL,
    legal_hierarchy      TEXT NOT NULL,
    document_type        TEXT,
    language             TEXT NOT NULL,
    raw_storage_uri      TEXT NOT NULL,
    s3_uri               TEXT,
    raw_format           TEXT,
    raw_checksum         TEXT NOT NULL,
    fetched_at           TIMESTAMPTZ NOT NULL,
    last_checked_at      TIMESTAMPTZ NOT NULL,
    valid_from           DATE,
    valid_to             DATE,
    superseded_by        UUID REFERENCES source_document(id),
    ingestion_pattern_id UUID,
    status               TEXT NOT NULL DEFAULT 'ingested',
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_sd_jurisdiction ON source_document(jurisdiction);
CREATE INDEX ix_sd_external_id  ON source_document(external_id);
CREATE INDEX ix_sd_publisher    ON source_document(publisher);
CREATE INDEX ix_sd_valid        ON source_document(valid_from, valid_to);


-- ============================================================================
-- provision  (the atomic unit of regulatory meaning)
-- ============================================================================
CREATE TABLE provision (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_document_id  UUID NOT NULL REFERENCES source_document(id) ON DELETE CASCADE,
    parent_id           UUID REFERENCES provision(id),
    hierarchy_path      LTREE NOT NULL,
    ordinal             INT NOT NULL,
    label               TEXT,
    eid                 TEXT,
    text                TEXT NOT NULL,
    text_format         TEXT NOT NULL DEFAULT 'gfm',
    ingestion_quality   REAL,
    text_hash           TEXT NOT NULL,
    language            TEXT NOT NULL,
    legal_hierarchy     TEXT NOT NULL,
    jurisdiction        TEXT NOT NULL,

    sector_tags         TEXT[] NOT NULL DEFAULT '{}',
    entity_tags         TEXT[] NOT NULL DEFAULT '{}',
    activity_tags       TEXT[] NOT NULL DEFAULT '{}',

    deontic_modal       TEXT,
    deontic_actor       TEXT,
    deontic_action      TEXT,
    deontic_condition   TEXT,
    is_definition       BOOLEAN NOT NULL DEFAULT FALSE,
    defined_terms       TEXT[] NOT NULL DEFAULT '{}',

    valid_from          DATE,
    valid_to            DATE,
    last_verified_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    embedding_semantic  VECTOR(1536),
    embedding_domain    VECTOR(1536),
    resolved_text       TEXT,
    embedding_resolved  VECTOR(1536),

    tsv                 tsvector
                          GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_p_source        ON provision(source_document_id);
CREATE INDEX ix_p_parent        ON provision(parent_id);
CREATE INDEX ix_p_path_gist     ON provision USING GIST (hierarchy_path);
CREATE INDEX ix_p_juris         ON provision(jurisdiction);
CREATE INDEX ix_p_legal_hier    ON provision(legal_hierarchy);
CREATE INDEX ix_p_sector        ON provision USING GIN (sector_tags);
CREATE INDEX ix_p_entity        ON provision USING GIN (entity_tags);
CREATE INDEX ix_p_activity      ON provision USING GIN (activity_tags);
CREATE INDEX ix_p_tsv           ON provision USING GIN (tsv);
CREATE INDEX ix_p_text_format   ON provision(text_format);
CREATE INDEX ix_p_valid         ON provision(valid_to) WHERE valid_to IS NULL;

-- HNSW indexes — built only after embedding generation (Day 6).
-- The CREATE INDEX statements are commented here for documentation; the
-- ingest/embed script builds them with CONCURRENTLY at the right moment.
-- CREATE INDEX ix_p_emb_semantic ON provision
--     USING hnsw (embedding_semantic vector_cosine_ops);
-- CREATE INDEX ix_p_emb_resolved ON provision
--     USING hnsw (embedding_resolved vector_cosine_ops)
--     WHERE embedding_resolved IS NOT NULL;


-- ============================================================================
-- provision_sentence  (sentence-level resolution, Section 12.4)
-- ============================================================================
CREATE TABLE provision_sentence (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    provision_id    UUID NOT NULL REFERENCES provision(id) ON DELETE CASCADE,
    sentence_index  INT NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'sentence',
    text            TEXT NOT NULL,
    embedding       VECTOR(1536),
    tsv             tsvector
                      GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    UNIQUE (provision_id, sentence_index)
);
CREATE INDEX ix_ps_provision ON provision_sentence(provision_id);
CREATE INDEX ix_ps_tsv       ON provision_sentence USING GIN (tsv);
-- HNSW built after embedding generation:
-- CREATE INDEX ix_ps_emb ON provision_sentence USING hnsw (embedding vector_cosine_ops);


-- ============================================================================
-- cross_reference
-- ============================================================================
CREATE TABLE cross_reference (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    from_provision  UUID NOT NULL REFERENCES provision(id) ON DELETE CASCADE,
    -- A cross-reference target may live in a different source document that
    -- gets re-ingested; keep the row but null the link rather than cascade.
    to_provision    UUID REFERENCES provision(id) ON DELETE SET NULL,
    to_external_ref TEXT,
    ref_type        TEXT NOT NULL,
    detected_by     TEXT NOT NULL,
    confidence      REAL NOT NULL,
    detected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_by     TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_xr_from     ON cross_reference(from_provision);
CREATE INDEX ix_xr_to       ON cross_reference(to_provision);
CREATE INDEX ix_xr_dangling ON cross_reference(to_external_ref) WHERE to_provision IS NULL;


-- ============================================================================
-- defined_term  (Section 12.8 — populated AdvMVP, table created at MVP)
-- ============================================================================
CREATE TABLE defined_term (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    term                     TEXT NOT NULL,
    scope_document_id        UUID REFERENCES source_document(id) ON DELETE CASCADE,
    definition_provision_id  UUID NOT NULL REFERENCES provision(id) ON DELETE CASCADE,
    definition_text          TEXT NOT NULL,
    extracted_by             TEXT NOT NULL,
    confidence               REAL NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_dt_term  ON defined_term(term);
CREATE INDEX ix_dt_scope ON defined_term(scope_document_id);


-- ============================================================================
-- project / project_document / project_context
-- ============================================================================
CREATE TABLE project (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id          UUID NOT NULL,
    name               TEXT NOT NULL,
    sector             TEXT NOT NULL,
    jurisdiction_scope TEXT[] NOT NULL,
    entity_type        TEXT,
    location           GEOMETRY(Point, 4326),
    location_polygon   GEOMETRY(Polygon, 4326),
    declared_intent    TEXT,
    declared_risks     TEXT[],
    risk_tolerance     TEXT NOT NULL DEFAULT 'moderate',
    analysis_depth     TEXT NOT NULL DEFAULT 'standard',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_project_client ON project(client_id);

CREATE TABLE project_document (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id         UUID NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL,
    raw_storage_uri    TEXT NOT NULL,
    extracted_text     TEXT,
    extracted_entities JSONB NOT NULL DEFAULT '{}'::jsonb,
    uploaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_pd_project ON project_document(project_id);

CREATE TABLE project_context (
    project_id            UUID PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    macro_summary         TEXT,
    applicable_provisions UUID[] NOT NULL DEFAULT '{}',
    meso_summaries        JSONB NOT NULL DEFAULT '{}'::jsonb,
    contradiction_tensor  JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_analyzed_at      TIMESTAMPTZ,
    analysis_state        TEXT NOT NULL DEFAULT 'pending'
);


-- ============================================================================
-- finding / hypothesis
-- ============================================================================
CREATE TABLE finding (
    id                         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id                 UUID NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    finding_type               TEXT NOT NULL,
    statement                  TEXT NOT NULL,
    severity                   TEXT NOT NULL,

    -- Citation: enforced non-empty by SQLAlchemy event hook AND by check below.
    cited_provisions           UUID[] NOT NULL,
    cited_documents            UUID[] NOT NULL,

    confidence_overall         REAL NOT NULL,
    confidence_source_coverage REAL,
    confidence_retrieval       REAL,
    confidence_interpretation  REAL,
    confidence_temporal        REAL,

    analysis_layer             TEXT NOT NULL,
    generated_by               TEXT NOT NULL,
    prompt_hash                TEXT,

    review_state               TEXT NOT NULL DEFAULT 'unreviewed',
    reviewed_by                UUID,
    reviewed_at                TIMESTAMPTZ,
    review_outcome             TEXT,
    superseded_by              UUID REFERENCES finding(id),

    payload                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT cited_provisions_nonempty
        CHECK (array_length(cited_provisions, 1) >= 1),
    CONSTRAINT cited_documents_nonempty
        CHECK (array_length(cited_documents, 1) >= 1)
);
CREATE INDEX ix_f_project  ON finding(project_id);
CREATE INDEX ix_f_severity ON finding(severity, review_state);
CREATE INDEX ix_f_type     ON finding(finding_type);

CREATE TABLE hypothesis (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id       UUID NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    statement        TEXT NOT NULL,
    polarity         TEXT NOT NULL,
    status           TEXT NOT NULL,
    confidence       REAL,
    evidence         JSONB NOT NULL DEFAULT '{}'::jsonb,
    derived_findings UUID[] NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ============================================================================
-- audit_event  (append-only from day one)
-- ============================================================================
CREATE TABLE audit_event (
    id            BIGSERIAL PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type    TEXT NOT NULL,
    actor_id      TEXT,
    action        TEXT NOT NULL,
    target_type   TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    project_id    UUID REFERENCES project(id),
    model_version TEXT,
    prompt_hash   TEXT,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_ae_target  ON audit_event(target_type, target_id);
CREATE INDEX ix_ae_project ON audit_event(project_id, occurred_at);
CREATE INDEX ix_ae_action  ON audit_event(action, occurred_at);

-- Make audit_event truly append-only at the DB level.
CREATE OR REPLACE FUNCTION audit_event_no_modify() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_event is append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_audit_event_no_update
    BEFORE UPDATE OR DELETE ON audit_event
    FOR EACH ROW EXECUTE FUNCTION audit_event_no_modify();


-- ============================================================================
-- retrieval_eval  (Section 12.7 — calibration set)
-- ============================================================================
CREATE TABLE retrieval_eval (
    id                     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query                  TEXT NOT NULL,
    expected_provision_id  UUID REFERENCES provision(id),
    retrieved_provision_id UUID REFERENCES provision(id),
    retrieval_score        REAL NOT NULL,
    rerank_score           REAL,
    label                  TEXT,
    labeled_by             TEXT,
    labeled_at             TIMESTAMPTZ,
    notes                  TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_re_label ON retrieval_eval(label);


-- ============================================================================
-- retrieval_cache  (HyDE / rewrite cache, 12-hour TTL)
-- ============================================================================
CREATE TABLE retrieval_cache (
    cache_key  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    value      JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_rc_expires ON retrieval_cache(expires_at);


COMMIT;
