-- 006_tenancy.sql
-- PHASE A · A2-T06 (WP-006) — tenancy spine, INERT behind tenancy_enabled=False.
--
-- Authoritative DDL: 02_Module_Polarius_Architecture_v5.md §2.2 (reconciled by
-- binder §16 / §8.C-4). This migration is ADDITIVE and changes NO behaviour:
--   * the new organization / "user" / membership tables are created empty;
--   * project.client_id is RENAMED IN PLACE to org_id (no data rip, no rewrite),
--     and the tenant FK is added NOT VALID so pre-existing project rows (whose
--     org_id still holds the per-request uuid4() client_id) are NOT checked here
--     — they are sentinel-backfilled and the FK VALIDATEd in 007;
--   * org_id is ADDED nullable to finding / audit_event / api_call (+ indexes).
--
-- Nothing is rewired: api/projects.py:54 still writes client_id=uuid4() into the
-- renamed org_id column (the GUC / Principal follow-up is WP-022 / A2-T22).
-- ADD COLUMN is permitted on append-only audit_event — its trigger blocks row
-- UPDATE/DELETE, not DDL (§2.2).
--
-- §8.C-4 / SA-06: organization carries org_type CHECK in (sme,advisor) DEFAULT sme
-- and a referred_by partner/AMSP attribution hook (binder §16.6).

BEGIN;

-- ============================================================================
-- organization  (NEW — RDS-3 §2.1 ; §8.C-4 org_type + SA-06 referred_by)
-- ============================================================================
CREATE TABLE organization (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),     -- stable internal id
    external_ref TEXT,                                            -- e.g. IČO; alongside, never PK
    name         TEXT NOT NULL,
    org_type     TEXT NOT NULL DEFAULT 'sme',                     -- 'sme' | 'advisor'
    country      TEXT NOT NULL DEFAULT 'CZ',
    status       TEXT NOT NULL DEFAULT 'active',                  -- 'active'|'suspended'|'closed'
    referred_by  TEXT,                                            -- SA-06 partner/AMSP attribution hook (binder §16.6)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT org_type_allowed CHECK (org_type IN ('sme', 'advisor'))
);
CREATE INDEX ix_org_external_ref ON organization(external_ref);
CREATE INDEX ix_org_type         ON organization(org_type);


-- ============================================================================
-- "user"  (NEW — quoted reserved word; Argon2id password_hash NULLABLE for SSO)
-- ============================================================================
CREATE TABLE "user" (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email             TEXT NOT NULL,
    email_verified    BOOLEAN NOT NULL DEFAULT FALSE,
    password_hash     TEXT,                                       -- Argon2id; nullable for SSO later
    display_name      TEXT,
    status            TEXT NOT NULL DEFAULT 'active',             -- 'active'|'disabled'
    is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at     TIMESTAMPTZ
);
-- case-insensitive uniqueness without citext (UNIQUE on lower(email))
CREATE UNIQUE INDEX ux_user_email ON "user"(lower(email));


-- ============================================================================
-- membership  (NEW — many-orgs-per-user; UNIQUE (user_id, org_id))
-- ============================================================================
CREATE TABLE membership (
    id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id    UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    org_id     UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member',                    -- 'owner'|'admin'|'member'|'advisor'
    seat_state TEXT NOT NULL DEFAULT 'active',                    -- 'active'|'invited'|'revoked' (drives seat billing)
    invited_by UUID REFERENCES "user"(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, org_id)
);
CREATE INDEX ix_mem_user ON membership(user_id);
CREATE INDEX ix_mem_org  ON membership(org_id);
CREATE INDEX ix_mem_seat ON membership(org_id, seat_state);


-- ============================================================================
-- AMEND project — rename client_id -> org_id IN PLACE + tenant FK NOT VALID
-- ============================================================================
-- The old per-request column (api/projects.py:54 client_id=uuid4()) becomes the
-- real tenant FK without a data rip. NOT VALID means existing rows are not
-- checked now (they still carry uuid4() values); 007 backfills the sentinel and
-- then VALIDATEs. The old ix_project_client index follows the column rename
-- automatically; we re-name it to ix_project_org for clarity.
ALTER TABLE project RENAME COLUMN client_id TO org_id;
ALTER INDEX ix_project_client RENAME TO ix_project_org;
ALTER TABLE project ADD CONSTRAINT fk_project_org
    FOREIGN KEY (org_id) REFERENCES organization(id) ON DELETE RESTRICT NOT VALID;  -- VALIDATE in 007


-- ============================================================================
-- AMEND finding / audit_event / api_call — add direct org_id (nullable) + index
-- ============================================================================
-- A direct org_id makes the tenant filter one predicate and lets api_call rows
-- with NULL project_id (embeddings, future Conduit calls) still attribute to a
-- tenant. Nullable here; 007 backfills finding.org_id and SET NOT NULL on it.
ALTER TABLE finding     ADD COLUMN org_id UUID;
CREATE INDEX ix_finding_org     ON finding(org_id);

ALTER TABLE audit_event ADD COLUMN org_id UUID;
CREATE INDEX ix_audit_event_org ON audit_event(org_id, occurred_at);

ALTER TABLE api_call    ADD COLUMN org_id UUID;
CREATE INDEX ix_api_call_org    ON api_call(org_id, ts DESC);

COMMIT;


-- ============================================================================
-- ROLLBACK (006_tenancy.sql)
-- ----------------------------------------------------------------------------
-- Destructive statements are confined to this rollback section only. Run this
-- block (uncommented) to fully reverse 006. Reverse order; restore the original
-- client_id column name and index. 007 must be rolled back first.
-- ============================================================================
-- BEGIN;
--
-- DROP INDEX IF EXISTS ix_api_call_org;
-- ALTER TABLE api_call    DROP COLUMN IF EXISTS org_id;
-- DROP INDEX IF EXISTS ix_audit_event_org;
-- ALTER TABLE audit_event DROP COLUMN IF EXISTS org_id;
-- DROP INDEX IF EXISTS ix_finding_org;
-- ALTER TABLE finding     DROP COLUMN IF EXISTS org_id;
--
-- ALTER TABLE project DROP CONSTRAINT IF EXISTS fk_project_org;
-- ALTER INDEX ix_project_org RENAME TO ix_project_client;
-- ALTER TABLE project RENAME COLUMN org_id TO client_id;
--
-- DROP TABLE IF EXISTS membership;
-- DROP TABLE IF EXISTS "user";
-- DROP TABLE IF EXISTS organization;
--
-- COMMIT;
