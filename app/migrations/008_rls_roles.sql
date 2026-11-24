-- 008_rls_roles.sql
-- v5 tenancy spine, Stage A (PERMISSIVE RLS) — A2-T08 · §2.11 / §16.5 AUTHORITATIVE.
--
-- This migration turns Row-Level Security ON for the four tenant-bearing tables
-- (project / finding / audit_event / api_call) but with a PERMISSIVE policy
-- USING(true) WITH CHECK(true): every row is still visible and writable. The
-- point is to install the *mechanism* (the app login role + RLS enabled + a
-- policy object on each table) so that the call sites can adopt SET LOCAL while
-- the product keeps working unchanged. The real tenant predicate is NOT applied
-- here — it lands in 015 (file-staged, NOT in this applied chain), which flips
-- `rls_enforced=True` in the same deploy after the AC-RLS-4 cross-tenant-leak
-- probe is green.
--
-- INERT GUARANTEE: behaviour is unchanged while `rls_enforced=False` (the only
-- consumer flag). With a permissive USING(true)/WITH CHECK(true) policy and a
-- role that does NOT have BYPASSRLS, regsuper_app reads and writes every row in
-- all four tables exactly as it does today. CARDINAL RULE (§2.11): the migration/
-- owner role keeps BYPASSRLS for migrations/backfills; regsuper_app does NOT —
-- a fail-OPEN BYPASSRLS on the app login would silently defeat 015.
--
-- NO RLS is enabled on the PUBLIC law tables (source_document / provision /
-- provision_sentence / cross_reference / defined_term / …): the corpus is public
-- and retrieval is cross-tenant by design. A coding agent must NOT "helpfully"
-- add RLS to those — it breaks retrieval (§2.11 PUBLIC list).
--
-- DEPENDS: 006 (organization / org_id columns on the four tables). 008 only
-- touches RLS state + the login role; it adds no columns and no tables.

BEGIN;

-- ============================================================================
-- DB login role for the application (the role TenancyMiddleware connects as).
-- NO BYPASSRLS — RLS applies to it. NOLOGIN-vs-LOGIN and the password are left
-- to the operator's environment (GRANT/ALTER ROLE … LOGIN out of band); this
-- migration only guarantees the role exists and is NOT a BYPASSRLS principal.
-- Idempotent: created only if absent so re-apply / shared clusters are safe.
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regsuper_app') THEN
        CREATE ROLE regsuper_app NOBYPASSRLS;
    ELSE
        -- Defensive: if the role pre-exists, make sure it is NOT a BYPASSRLS
        -- principal (a BYPASSRLS app login would fail-OPEN under 015).
        ALTER ROLE regsuper_app NOBYPASSRLS;
    END IF;
END
$$;

-- The app role needs table privileges; RLS narrows WHAT it can see, GRANT
-- decides the operation set. Owner keeps BYPASSRLS implicitly (table owner is
-- exempt unless FORCE ROW LEVEL SECURITY — we do NOT force it here, so
-- migrations/backfills run unhindered as owner).
GRANT SELECT, INSERT, UPDATE, DELETE ON project       TO regsuper_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON finding       TO regsuper_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON audit_event   TO regsuper_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON api_call      TO regsuper_app;
GRANT USAGE, SELECT ON SEQUENCE audit_event_id_seq    TO regsuper_app;

-- ============================================================================
-- Stage A: ENABLE RLS + PERMISSIVE USING(true) WITH CHECK(true) on the four
-- tenant tables. PERMISSIVE (the default policy kind) means this policy ORs
-- with any later policy; on its own USING(true) admits every row, so the app
-- is unaffected while `rls_enforced=False`.
-- ============================================================================

-- project ---------------------------------------------------------------------
ALTER TABLE project ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_project_permissive ON project
    AS PERMISSIVE FOR ALL TO regsuper_app
    USING (true) WITH CHECK (true);

-- finding ---------------------------------------------------------------------
ALTER TABLE finding ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_finding_permissive ON finding
    AS PERMISSIVE FOR ALL TO regsuper_app
    USING (true) WITH CHECK (true);

-- audit_event -----------------------------------------------------------------
-- NB: audit_event already carries the append-only trg_audit_event_no_update
-- trigger (001) — RLS is orthogonal to it; the permissive policy does not relax
-- append-only.
ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_audit_event_permissive ON audit_event
    AS PERMISSIVE FOR ALL TO regsuper_app
    USING (true) WITH CHECK (true);

-- api_call --------------------------------------------------------------------
ALTER TABLE api_call ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_api_call_permissive ON api_call
    AS PERMISSIVE FOR ALL TO regsuper_app
    USING (true) WITH CHECK (true);

-- ============================================================================
-- The PUBLIC law tables are deliberately LEFT WITHOUT RLS (source_document,
-- provision, provision_sentence, cross_reference, defined_term, retrieval_eval,
-- retrieval_cache). Retrieval is cross-tenant by design. Do NOT add RLS here.
-- ============================================================================

COMMIT;
