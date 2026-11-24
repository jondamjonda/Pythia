-- 015_rls_enforce.sql
-- PHASE3C · PHASE A · A2-T15 — RLS Stage B: drop the 008 permissive policies and
-- install the tenant predicate across the FULL §16.5 enforced list.
-- (doc-02 §2.11 Stage B, §16.5 AUTHORITATIVE.)
--
-- ============================================================================
-- ✅  KNOWN-INCOMPLETE FIXES — RESOLVED 2026-06-29 (both now self-contained in §0 below).
--   (1) ENABLE RLS now precedes the policies for ALL enforced tables (§0.2). 008
--       enables RLS on the core four; §0.2 enables it on the other nine, so no
--       CREATE POLICY lands on an RLS-disabled (inert) table. Mirrored by DISABLE
--       in the rollback.
--   (2) project_document / project_context / hypothesis (project_id only) AND
--       advisor_client (advisor_id only) gain org_id + a backfill + NOT NULL + an
--       index in §0.1, BEFORE their policies — so none ERRORs "column org_id does
--       not exist". advisor_client was an additional gap the original note missed.
--   AC-RLS-4 (the cross-tenant leak gate) is GREEN: scripts/run_rls_leak_test.sh
--   proves this exact predicate isolates tenants (read + write + no-GUC + admin).
-- ============================================================================
--
-- ============================================================================
-- 📍  APPLIED-STATE (LEDGER-M17) — read with the FILE-STAGED banner below.
--     • LOCAL / dev DB: 015 IS APPLIED + ENFORCING. The run scripts
--       (run_local.sh etc.) connect as `regsuper_app` (NOBYPASSRLS) with
--       RLS_ENFORCED=1 + TENANCY_ENABLED=1, so the predicate below is live and
--       isolating today. The 16 tenant policies exist on the local DB.
--     • CLEAN / PROD rebuild: 015 stays FILE-STAGED (NOT in the applied chain) —
--       it must only be applied as part of the coupled Phase-C flip described
--       below (AC-RLS-4 GREEN + rls_enforced flipped in the SAME deploy), so a
--       fresh DB never lands enforced-but-un-wired.
--     This migration now also lives under migrations/staged/ (LEDGER-C5) so
--     Postgres initdb cannot auto-apply it on a clean `docker compose up`.
-- ============================================================================
--
-- ============================================================================
-- ⚠️  FILE-STAGED for clean/prod rebuilds — NOT IN THE APPLIED CHAIN.  The
--     migration harness (scripts/apply_migrations.py) MUST EXCLUDE 015 and 016:
--     it asserts the file is present, dry-parses it, and proves the rollback
--     restores 008 — but it NEVER runs 015 against prod or copy-of-prod as part
--     of 006→014.  (LOCAL is the exception above: applied via the run scripts.)
--
--     CARDINAL RULE: this migration enforces tenant isolation.  Applying it
--     FLIPS the system from "all rows visible" to "rows visible only under the
--     org GUC".  It MUST NOT be applied until AC-RLS-4 is GREEN (the SET LOCAL
--     app.current_org plumbing proven end-to-end) AND rls_enforced is flipped
--     to True in the SAME deploy.  Applied early — before the GUC is wired — it
--     locks every read (the predicate sees a NULL GUC and admits nothing).
--     That is failure-mode #1 (RLS-GUC-leak / lockout); the file-staged status
--     is the guard.
-- ============================================================================
--
-- The predicate (identical for every enforced table, NULL-safe):
--   org_id = current_setting('app.current_org', true)::uuid
--   OR current_setting('app.is_platform_admin', true) = 'on'
-- The `true` second arg to current_setting makes a missing GUC return NULL
-- (not error); a NULL GUC therefore admits NO tenant rows and admits
-- system/NULL-org rows only to a platform admin.  Policies are FOR ALL TO
-- regsuper_app (the app login; the owner/migration role keeps BYPASSRLS).
--
-- PUBLIC tables (source_document, provision, provision_sentence,
-- cross_reference, defined_term, provision_id_map, plan, entitlement,
-- disclaimer) get NO RLS — the law is public and retrieval is cross-tenant by
-- design.  This migration does NOT touch them.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0.1 (FIX 2) — org_id on the tables that lack it, so the org-scoped policy can apply. Idempotent
--     (ADD COLUMN IF NOT EXISTS + backfill the NULLs + set NOT NULL + index). The 3 project-family tables
--     backfill from the owning project; advisor_client backfills from its advisor. The backfill runs as the
--     migration OWNER (BYPASSRLS), so it sees every row before enforcement is installed below.
-- ---------------------------------------------------------------------------
ALTER TABLE project_document ADD COLUMN IF NOT EXISTS org_id uuid;
UPDATE project_document d SET org_id = p.org_id FROM project p WHERE d.project_id = p.id AND d.org_id IS NULL;
ALTER TABLE project_document ALTER COLUMN org_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_project_document_org_id ON project_document (org_id);

ALTER TABLE project_context ADD COLUMN IF NOT EXISTS org_id uuid;
UPDATE project_context c SET org_id = p.org_id FROM project p WHERE c.project_id = p.id AND c.org_id IS NULL;
ALTER TABLE project_context ALTER COLUMN org_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_project_context_org_id ON project_context (org_id);

ALTER TABLE hypothesis ADD COLUMN IF NOT EXISTS org_id uuid;
UPDATE hypothesis h SET org_id = p.org_id FROM project p WHERE h.project_id = p.id AND h.org_id IS NULL;
ALTER TABLE hypothesis ALTER COLUMN org_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_hypothesis_org_id ON hypothesis (org_id);

ALTER TABLE advisor_client ADD COLUMN IF NOT EXISTS org_id uuid;
UPDATE advisor_client ac SET org_id = a.org_id FROM advisor a WHERE ac.advisor_id = a.id AND ac.org_id IS NULL;
ALTER TABLE advisor_client ALTER COLUMN org_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS ix_advisor_client_org_id ON advisor_client (org_id);

-- ---------------------------------------------------------------------------
-- 0.2 (FIX 1) — ENABLE RLS on the nine enforced tables 008 did NOT cover (008 enables only the core four:
--     project/finding/audit_event/api_call). A CREATE POLICY on a table WITHOUT RLS enabled is INERT, so this
--     MUST run before the policies below or they would silently fail to isolate. Mirrored by DISABLE in rollback.
-- ---------------------------------------------------------------------------
ALTER TABLE business_profile  ENABLE ROW LEVEL SECURITY;
ALTER TABLE advisor           ENABLE ROW LEVEL SECURITY;
ALTER TABLE advisor_client    ENABLE ROW LEVEL SECURITY;
ALTER TABLE handoff           ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_export      ENABLE ROW LEVEL SECURITY;
ALTER TABLE analysis_run      ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_counter     ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscription      ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_event     ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_document  ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_context   ENABLE ROW LEVEL SECURITY;
ALTER TABLE hypothesis        ENABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 0.3 (LEDGER-C1) — FORCE ROW LEVEL SECURITY on EVERY enforced table.
--     Postgres exempts a table's OWNER from RLS unless the table is FORCEd, even
--     when RLS is ENABLED. The live app's DATABASE_URL default connects as the
--     `regsuper` owner (the role that ran the migrations); without FORCE, that
--     connection bypasses every tenant policy below and sees ALL rows — i.e.
--     `rls_enforced=True` would NOT isolate tenants. FORCE makes the predicate
--     bind the owner too. (The app SHOULD still connect as `regsuper_app`; FORCE
--     is the belt-and-braces guard against the owner-DSN default. A boot guard
--     that refuses rls_enforced=True under a BYPASSRLS/owner role is the Python
--     half of LEDGER-C1, tracked separately — see needs_attention.) Mirrored by
--     NO FORCE in the rollback. Idempotent: FORCE is a no-op if already forced.
-- ---------------------------------------------------------------------------
ALTER TABLE project           FORCE ROW LEVEL SECURITY;
ALTER TABLE finding           FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_event       FORCE ROW LEVEL SECURITY;
ALTER TABLE api_call          FORCE ROW LEVEL SECURITY;
ALTER TABLE business_profile  FORCE ROW LEVEL SECURITY;
ALTER TABLE advisor           FORCE ROW LEVEL SECURITY;
ALTER TABLE advisor_client    FORCE ROW LEVEL SECURITY;
ALTER TABLE handoff           FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_export      FORCE ROW LEVEL SECURITY;
ALTER TABLE analysis_run      FORCE ROW LEVEL SECURITY;
ALTER TABLE usage_counter     FORCE ROW LEVEL SECURITY;
ALTER TABLE subscription      FORCE ROW LEVEL SECURITY;
ALTER TABLE billing_event     FORCE ROW LEVEL SECURITY;
ALTER TABLE project_document  FORCE ROW LEVEL SECURITY;
ALTER TABLE project_context   FORCE ROW LEVEL SECURITY;
ALTER TABLE hypothesis        FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- 1. Drop the 008 permissive policies (USING(true) WITH CHECK(true)).
--    008 named each `p_<table>_permissive`; this migration replaces them with
--    `p_<table>_tenant`.  RLS stays ENABLED on each table throughout.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS p_project_permissive          ON project;
DROP POLICY IF EXISTS p_finding_permissive          ON finding;
DROP POLICY IF EXISTS p_audit_event_permissive      ON audit_event;
DROP POLICY IF EXISTS p_api_call_permissive         ON api_call;
DROP POLICY IF EXISTS p_business_profile_permissive ON business_profile;
DROP POLICY IF EXISTS p_advisor_permissive          ON advisor;
DROP POLICY IF EXISTS p_advisor_client_permissive   ON advisor_client;
DROP POLICY IF EXISTS p_handoff_permissive          ON handoff;
DROP POLICY IF EXISTS p_audit_export_permissive     ON audit_export;
DROP POLICY IF EXISTS p_analysis_run_permissive     ON analysis_run;
DROP POLICY IF EXISTS p_usage_counter_permissive    ON usage_counter;
DROP POLICY IF EXISTS p_subscription_permissive     ON subscription;
DROP POLICY IF EXISTS p_billing_event_permissive    ON billing_event;
-- Defence-in-depth (client-uploaded data; org_id added by 006/010):
DROP POLICY IF EXISTS p_project_document_permissive ON project_document;
DROP POLICY IF EXISTS p_project_context_permissive  ON project_context;
DROP POLICY IF EXISTS p_hypothesis_permissive       ON hypothesis;

-- ---------------------------------------------------------------------------
-- 2. Install the tenant predicate across the FULL §16.5 enforced list.
--    (RLS was already ENABLED by 008 on each of these tables; we only swap the
--    policy from permissive to the org-scoped predicate.)
-- ---------------------------------------------------------------------------

CREATE POLICY p_project_tenant ON project FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_finding_tenant ON finding FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_audit_event_tenant ON audit_event FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_api_call_tenant ON api_call FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_business_profile_tenant ON business_profile FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_advisor_tenant ON advisor FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_advisor_client_tenant ON advisor_client FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_handoff_tenant ON handoff FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_audit_export_tenant ON audit_export FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_analysis_run_tenant ON analysis_run FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_usage_counter_tenant ON usage_counter FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_subscription_tenant ON subscription FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_billing_event_tenant ON billing_event FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

-- Defence-in-depth: same predicate on client-uploaded data.
CREATE POLICY p_project_document_tenant ON project_document FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_project_context_tenant ON project_context FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

CREATE POLICY p_hypothesis_tenant ON hypothesis FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

COMMIT;


-- ===========================================================================
-- ROLLBACK — 015_rls_enforce.sql  →  restores the 008 permissive state.
-- (Drops the tenant policies and re-creates the permissive USING(true)
--  WITH CHECK(true) policies exactly as 008 left them.  RLS stays ENABLED on
--  every table; only the policy body reverts, so no row is lost and every row
--  becomes visible again regardless of the GUC.  DROP POLICY only — never
--  DROP TABLE.)
-- ===========================================================================
-- BEGIN;
--
-- -- 1. Drop the enforced tenant policies.
-- DROP POLICY IF EXISTS p_project_tenant          ON project;
-- DROP POLICY IF EXISTS p_finding_tenant          ON finding;
-- DROP POLICY IF EXISTS p_audit_event_tenant      ON audit_event;
-- DROP POLICY IF EXISTS p_api_call_tenant         ON api_call;
-- DROP POLICY IF EXISTS p_business_profile_tenant ON business_profile;
-- DROP POLICY IF EXISTS p_advisor_tenant          ON advisor;
-- DROP POLICY IF EXISTS p_advisor_client_tenant   ON advisor_client;
-- DROP POLICY IF EXISTS p_handoff_tenant          ON handoff;
-- DROP POLICY IF EXISTS p_audit_export_tenant     ON audit_export;
-- DROP POLICY IF EXISTS p_analysis_run_tenant     ON analysis_run;
-- DROP POLICY IF EXISTS p_usage_counter_tenant    ON usage_counter;
-- DROP POLICY IF EXISTS p_subscription_tenant     ON subscription;
-- DROP POLICY IF EXISTS p_billing_event_tenant    ON billing_event;
-- DROP POLICY IF EXISTS p_project_document_tenant ON project_document;
-- DROP POLICY IF EXISTS p_project_context_tenant  ON project_context;
-- DROP POLICY IF EXISTS p_hypothesis_tenant       ON hypothesis;
--
-- -- 2a. NO FORCE on every table §0.3 forced (restore 008, which never FORCEd; the owner regains its bypass).
-- --     Run BEFORE the DISABLE below. Idempotent: NO FORCE is a no-op if not currently forced.
-- ALTER TABLE project           NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE finding           NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE audit_event       NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE api_call          NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE business_profile  NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE advisor           NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE advisor_client    NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE handoff           NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE audit_export      NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE analysis_run      NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE usage_counter     NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE subscription      NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE billing_event     NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE project_document  NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE project_context   NO FORCE ROW LEVEL SECURITY;
-- ALTER TABLE hypothesis        NO FORCE ROW LEVEL SECURITY;
--
-- -- 2. DISABLE RLS on the nine tables §0.2 enabled (restore 008, which enabled RLS only on the core four).
-- --    The org_id columns §0.1 added are KEPT (additive, backfilled, inert when RLS is off) — restoring 008
-- --    does not require dropping data; re-applying 015 is idempotent (ADD COLUMN IF NOT EXISTS).
-- ALTER TABLE business_profile  DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE advisor           DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE advisor_client    DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE handoff           DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE audit_export      DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE analysis_run      DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE usage_counter     DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE subscription      DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE billing_event     DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE project_document  DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE project_context   DISABLE ROW LEVEL SECURITY;
-- ALTER TABLE hypothesis        DISABLE ROW LEVEL SECURITY;
--
-- -- 3. Re-create the 008 permissive policies on the core four (RLS stays ENABLED on these; 008's state).
-- CREATE POLICY p_project_permissive     ON project     FOR ALL TO regsuper_app USING (true) WITH CHECK (true);
-- CREATE POLICY p_finding_permissive     ON finding     FOR ALL TO regsuper_app USING (true) WITH CHECK (true);
-- CREATE POLICY p_audit_event_permissive ON audit_event FOR ALL TO regsuper_app USING (true) WITH CHECK (true);
-- CREATE POLICY p_api_call_permissive    ON api_call    FOR ALL TO regsuper_app USING (true) WITH CHECK (true);
--
-- COMMIT;
