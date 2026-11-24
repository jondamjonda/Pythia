-- 013_trust_audit_export.sql
-- PHASE3C · Phase A · A2-T13 / WP-013 — provision trust columns + audit_export + session (INERT).
--
-- Purely ADDITIVE; changes NO behaviour. It opens three seams:
--   1. Four trust/temporal columns on `provision`, ALL NULLABLE until the WP-055
--      backfill (scripts/backfill_trust_fields.py + ingest/trust_fields.py) runs.
--      A null-trust provision yields NULL trust at runtime — never a fabricated
--      "hard-law" badge (§4.2 failure-mode 6, trust-vocab-mismatch).
--   2. audit_export — the EU-AI-Act artifact record; content-immutable.
--   3. session — server-side opaque-token sessions; the GUC source WP-022 reads.
--
-- VOCAB SOURCE (load-bearing): the CHECK constraints encode the D1-trust-vocab
-- RESOLVED set from PHASE3C_A1_Decisions_Register.md (A1-T11 / WP-001) — the doc-04
-- §2.2 4-value HYPHENATED set. The doc-02 §2.9 5-value UNDERSCORE example
-- (hard_law/soft_law/guidance/recital/definition, in_force/future/.../transitional)
-- is SUPERSEDED and must NOT be encoded. Hyphens + single-quoting are load-bearing:
-- 'hard-law' not 'hard_law', 'in-force' not 'in_force', 'future-effective' not 'future'.
--
-- Source DDL: 02_Module_Polarius_Architecture_v5.md §2.9 (table/column shapes) — but
-- the WP-001 RESOLVED vocab OVERRIDES doc-02's CHECK literals, per authority order.

BEGIN;

-- ============================================================================
-- AMEND provision — four trust columns (RDS-6 §2). ALL NULLABLE until WP-055.
-- ----------------------------------------------------------------------------
-- bindingness_tier  ∈ {'hard-law','oop','soft-law','interpretive'}  ('oop' = Official
--                     Position Paper). NULLABLE — CHECK admits NULL.
-- temporal_status   ∈ {'in-force','future-effective','annulled','superseded'}. NULLABLE.
-- consolidated_informative  BOOLEAN NOT NULL DEFAULT FALSE — not enum-constrained.
-- in_force_as_of_note       TEXT — free-text; not enum-constrained.
--
-- The CHECKs allow NULL so every existing provision passes unchanged; derive_trust_fields
-- populates them deterministically (no LLM) at the WP-055 backfill.
-- ============================================================================
ALTER TABLE provision
    ADD COLUMN IF NOT EXISTS bindingness_tier         TEXT
        CHECK (bindingness_tier IN ('hard-law','oop','soft-law','interpretive')
               OR bindingness_tier IS NULL),
    ADD COLUMN IF NOT EXISTS temporal_status          TEXT
        CHECK (temporal_status IN ('in-force','future-effective','annulled','superseded')
               OR temporal_status IS NULL),
    ADD COLUMN IF NOT EXISTS consolidated_informative BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS in_force_as_of_note      TEXT;

-- Partial indexes — only the (eventually) populated rows are indexed, so this
-- stays cheap while the columns are all-NULL.
CREATE INDEX IF NOT EXISTS ix_p_bindingness ON provision(bindingness_tier) WHERE bindingness_tier IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_p_temporal    ON provision(temporal_status)  WHERE temporal_status  IS NOT NULL;


-- ============================================================================
-- NEW audit_export — the EU-AI-Act export artifact record (RDS-6 §1).
-- ----------------------------------------------------------------------------
-- Content-immutable. payload_hash = SHA-256 of the canonical JSON (tamper evidence).
-- GDPR erasure is a SEPARATE tombstone/redaction record (binder §16.7, INV-5) — it
-- is NEVER a mutation of this table. The no-delete trigger below is DISTINCT from
-- audit_event_no_modify (001): it ALLOWS UPDATE (so status can advance
-- pending->generating->done) but BLOCKS DELETE. Blocking UPDATE here would break
-- the legitimate status progression; blocking only DELETE preserves immutability of
-- the record while letting the export worker mark it done.
--
-- Tenant-scoped: §16.5 RLS is ENFORCED in the file-staged migration 015.
-- ============================================================================
CREATE TABLE IF NOT EXISTS audit_export (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    export_type    TEXT NOT NULL CHECK (export_type IN ('analysis','finding')),
    subject_id     UUID NOT NULL,                             -- project_id OR finding_id
    org_id         UUID NOT NULL REFERENCES organization(id), -- tenancy (RLS in 015)
    requested_by   UUID NOT NULL,                             -- "user".id
    requested_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    generated_at   TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','generating','done','error')),
    schema_version TEXT NOT NULL DEFAULT '1.0',
    json_s3_uri    TEXT,                                      -- s3://bucket/exports/{id}.json
    pdf_s3_uri     TEXT,                                      -- s3://bucket/exports/{id}.pdf
    error_detail   TEXT,
    payload_hash   TEXT,                                      -- SHA-256 of canonical JSON (tamper evidence)
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_ae_exp_subject ON audit_export(subject_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS ix_ae_exp_org     ON audit_export(org_id, requested_at DESC);
CREATE INDEX IF NOT EXISTS ix_ae_exp_status  ON audit_export(status) WHERE status IN ('pending','generating');

-- no-DELETE trigger: ALLOW UPDATE (status can advance), BLOCK DELETE (immutable record).
-- Distinct from audit_event_no_modify (001), which blocks BOTH update and delete.
CREATE OR REPLACE FUNCTION audit_export_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_export rows are immutable (no delete)';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_audit_export_no_delete ON audit_export;
CREATE TRIGGER trg_audit_export_no_delete
    BEFORE DELETE ON audit_export
    FOR EACH ROW EXECUTE FUNCTION audit_export_no_delete();


-- ============================================================================
-- NEW session — server-side opaque-token product sessions (RDS-3 §2.7).
-- ----------------------------------------------------------------------------
-- token_hash = sha256 of the opaque token (the secret itself is NEVER stored),
-- UNIQUE. active_org_id is the org this session "acts as" (an advisor can switch
-- orgs) — this column is the GUC source WP-022 (api/auth.py) reads to set the
-- per-request tenant. The ORM class for this table MUST be ALIASED (e.g.
-- UserSession) to avoid clashing with sqlalchemy.orm.Session, which storage/
-- audit.py:20 already imports as `Session` for its before_flush listener.
-- ============================================================================
CREATE TABLE IF NOT EXISTS session (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    token_hash    TEXT NOT NULL UNIQUE,                       -- sha256 of opaque token (secret never stored)
    active_org_id UUID REFERENCES organization(id),           -- org this session "acts as" (advisor switches)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ,
    ip            TEXT,
    user_agent    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_session_token ON session(token_hash);
CREATE INDEX IF NOT EXISTS ix_session_user   ON session(user_id);
CREATE INDEX IF NOT EXISTS ix_session_expiry ON session(expires_at);

COMMIT;


-- ============================================================================
-- ROLLBACK (013_trust_audit_export.sql)
-- ----------------------------------------------------------------------------
-- Destructive statements are confined to this section, per the additive rule.
-- Apply manually to undo 013. Drop the session/audit_export tables and the
-- trigger function, then drop the four provision trust columns.
-- ============================================================================
-- BEGIN;
--
-- DROP TABLE IF EXISTS session;
--
-- DROP TRIGGER IF EXISTS trg_audit_export_no_delete ON audit_export;
-- DROP TABLE IF EXISTS audit_export;
-- DROP FUNCTION IF EXISTS audit_export_no_delete();
--
-- DROP INDEX IF EXISTS ix_p_temporal;
-- DROP INDEX IF EXISTS ix_p_bindingness;
-- ALTER TABLE provision
--     DROP COLUMN IF EXISTS in_force_as_of_note,
--     DROP COLUMN IF EXISTS consolidated_informative,
--     DROP COLUMN IF EXISTS temporal_status,
--     DROP COLUMN IF EXISTS bindingness_tier;
--
-- COMMIT;
