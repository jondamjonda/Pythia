-- 010_profiles_advisor.sql
-- v5 business profile + advisor workspace — A2-T10 · §2.3 / §16.5.
--
-- `business_profile` is the SME described ONCE: legal form, IČO, NACE code,
-- headcount band, and the FIVE boolean compliance gates that drive L0 tag
-- injection. One business_profile maps to many project rows (analysis runs);
-- the project table is otherwise untouched apart from a NULLABLE FK.
--
-- INERT GUARANTEE: nothing reads these tables until the profile/advisor surfaces
-- ship, gated behind `tenancy_enabled=False` (default; §config A1-T01). The new
-- `project.business_profile_id` is NULLABLE (existing project rows stay valid;
-- making it NOT NULL would break projects.py — explicitly avoided per A2-T10).
--
-- DERIVED-TAGS RULE (load-bearing): `derived_sector_tags` and
-- `derived_activity_tags` are L0-WRITE-ONLY — they are written EXCLUSIVELY by
-- the L0 `derive_tags_for_profile()` deterministic deriver (§4.1), NEVER by the
-- UI / intake form. The intake schema (BusinessProfileIntake) must NOT expose
-- them. A non-empty value here that did not come from L0 would mislead the L1
-- candidate filter; the columns exist so L0 can persist its output, not so a
-- client can inject tags.
--
-- RLS: `business_profile`, `advisor`, `advisor_client` are tenant-bearing and on
-- the §16.5 ENFORCED RLS list — the org_id predicate is added at 015 (NOT here).
-- 008 already enabled permissive RLS only on project/finding/audit_event/
-- api_call; these three tables get RLS first at 015. No RLS is added here.
--
-- DEPENDS: 006 (organization — FK target of business_profile.org_id and
-- advisor.org_id), 001 (project — ALTER target).

BEGIN;

-- ============================================================================
-- business_profile  (the SME described once; tenant-bearing — RLS at 015)
-- ============================================================================
CREATE TABLE business_profile (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id                  UUID REFERENCES organization(id) ON DELETE CASCADE,  -- tenancy (RLS in 015)
    -- identity
    display_name            TEXT NOT NULL,
    legal_name              TEXT,
    ico                     TEXT,                                  -- IČO 8-digit string (external, not PK)
    legal_form              TEXT NOT NULL,                         -- 'sro'|'as'|'osvč'|'ks'|'vos'|'se'|'družstvo'|'other'
    -- sector / activity
    nace_code               TEXT,                                  -- 4-digit NACE Rev 2
    nace_description        TEXT,
    sector_override         TEXT,                                  -- maps to SECTOR_TO_TAGS key; NULL = derive from NACE
    additional_activities   TEXT[] NOT NULL DEFAULT '{}',
    -- scale
    headcount_band          TEXT NOT NULL DEFAULT '2-9',           -- '1'|'2-9'|'10-49'|'50-249'|'250+'
    annual_turnover_czk     BIGINT,
    -- compliance gates (drive L0 tag injection + applicability) — the FIVE gates
    processes_personal_data BOOLEAN NOT NULL DEFAULT FALSE,        -- GDPR gate
    handles_client_money    BOOLEAN NOT NULL DEFAULT FALSE,        -- AML/PSP gate
    has_real_estate         BOOLEAN NOT NULL DEFAULT FALSE,        -- Building Act / cadastre gate (Stage 2)
    cross_border            BOOLEAN NOT NULL DEFAULT FALSE,        -- CZ+EU dual-jurisdiction
    is_employer             BOOLEAN NOT NULL DEFAULT FALSE,        -- Labour Act gate
    regulated_activity      TEXT,
    -- jurisdiction + prefs
    primary_jurisdiction    TEXT NOT NULL DEFAULT 'CZ',            -- 'CZ'|'EU'|'CZ+EU'
    registered_municipality TEXT,                                  -- future PostGIS
    preferred_language      TEXT NOT NULL DEFAULT 'cs',            -- 'cs'|'en'
    analysis_depth          TEXT NOT NULL DEFAULT 'standard',
    -- derived tags (WRITTEN BY L0 derive_tags_for_profile() ONLY, never the UI)
    derived_sector_tags     TEXT[] NOT NULL DEFAULT '{}',
    derived_activity_tags   TEXT[] NOT NULL DEFAULT '{}',
    tags_version            INT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX ix_bp_org   ON business_profile(org_id);
CREATE INDEX ix_bp_ico   ON business_profile(ico)       WHERE ico IS NOT NULL;
CREATE INDEX ix_bp_nace  ON business_profile(nace_code) WHERE nace_code IS NOT NULL;
CREATE INDEX ix_bp_legal ON business_profile(legal_form);

-- updated_at touch trigger.
CREATE OR REPLACE FUNCTION bp_set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END; $$;
CREATE TRIGGER trg_bp_updated_at BEFORE UPDATE ON business_profile
    FOR EACH ROW EXECUTE FUNCTION bp_set_updated_at();

-- One business_profile → many project rows (analysis runs). project table not
-- otherwise altered. Column is NULLABLE — existing project rows stay valid and
-- projects.py is unchanged (a NOT NULL here would break project creation).
ALTER TABLE project ADD COLUMN IF NOT EXISTS business_profile_id UUID
    REFERENCES business_profile(id) ON DELETE SET NULL;
CREATE INDEX ix_proj_bp ON project(business_profile_id) WHERE business_profile_id IS NOT NULL;

-- ============================================================================
-- advisor  (the účetní / lawyer running a multi-client workspace; RLS at 015)
-- ============================================================================
CREATE TABLE advisor (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id       UUID REFERENCES organization(id) ON DELETE CASCADE,   -- advisor's OWN org (org_type='advisor')
    display_name TEXT NOT NULL,
    email        TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'accountant',                   -- 'accountant'|'lawyer'|'admin'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX ix_advisor_email ON advisor(email);

-- ============================================================================
-- advisor_client  (the advisor → client-profile grant; RLS at 015)
-- ============================================================================
CREATE TABLE advisor_client (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    advisor_id          UUID NOT NULL REFERENCES advisor(id) ON DELETE CASCADE,
    business_profile_id UUID NOT NULL REFERENCES business_profile(id) ON DELETE CASCADE,
    granted_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by          TEXT,
    access_level        TEXT NOT NULL DEFAULT 'read',                  -- 'read'|'edit'|'full'
    UNIQUE (advisor_id, business_profile_id)
);
CREATE INDEX ix_advisor_client_advisor ON advisor_client(advisor_id);
CREATE INDEX ix_advisor_client_profile ON advisor_client(business_profile_id);

COMMIT;
