-- AC-RLS-4 — cross-tenant leak test (the gate for rls_enforced + migration 015).
--
-- Proves the migration-015 tenant predicate isolates tenants under real Postgres RLS, using the real role
-- model: regsuper_app is NOBYPASSRLS (RLS applies); the owner is exempt (seeds data). We act as the app login
-- via SET ROLE regsuper_app (which activates RLS for the session, exactly like the app connecting as that role).
--
-- Coverage (LEDGER-M18): the read/write isolation tests T1-T5 run on project/finding; T6-T8 additionally
-- exercise NULL-org WRITE REJECTION on the cascade/observability write tables audit_event, api_call, and
-- handoff — the paths that previously dropped org_id (LEDGER-C3/H6). Tables are FORCEd (LEDGER-C1) to match the
-- migration as deployed.
--
-- Run against an ISOLATED scratch DB only (see scripts/run_rls_leak_test.sh). Any leak RAISEs EXCEPTION; with
-- ON_ERROR_STOP=1 that exits psql non-zero. A clean run prints PASS notices and exits 0.
--
-- The predicate under test is copied VERBATIM from migrations/015_rls_enforce.sql:
--   org_id = current_setting('app.current_org', true)::uuid
--   OR current_setting('app.is_platform_admin', true) = 'on'

\set ON_ERROR_STOP on

-- ── minimal tenant tables (only the columns the 015 predicate references) ───────────────────────────────────
CREATE TABLE organization (id uuid PRIMARY KEY);
CREATE TABLE project (id uuid PRIMARY KEY, org_id uuid NOT NULL);
CREATE TABLE finding (id uuid PRIMARY KEY, org_id uuid NOT NULL, project_id uuid, statement text);
-- NULL-org write-rejection tables (LEDGER-M18). These three model the WRITE PATHS that the cascade /
-- observability code can drive with a NULL org_id (LEDGER-C3/H6/M16). Their org_id is left NULLABLE on purpose
-- (it mirrors today's real schema — exactly the M16 latent gap) so that an INSERT with org_id=NULL reaches the
-- 015 WITH CHECK and is REJECTED there (NULL = <uuid> is NULL, never true → fail-closed), proving the policy —
-- not a NOT NULL constraint — is what catches the NULL-org write.
CREATE TABLE audit_event (id uuid PRIMARY KEY, org_id uuid, event_type text);
CREATE TABLE api_call    (id uuid PRIMARY KEY, org_id uuid, project_id uuid);
CREATE TABLE handoff     (id uuid PRIMARY KEY, org_id uuid, contact_payload text);

-- ── the app login role: NOBYPASSRLS (008_rls_roles.sql) ─────────────────────────────────────────────────────
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regsuper_app') THEN
    CREATE ROLE regsuper_app NOBYPASSRLS;
  END IF;
END $$;
ALTER ROLE regsuper_app NOBYPASSRLS;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON organization, project, finding, audit_event, api_call, handoff TO regsuper_app;

-- ── ENABLE + FORCE RLS (008 + 015 §0.3) + the 015 tenant predicate (verbatim) ───────────────────────────────
-- FORCE (LEDGER-C1) is added here too so the test exercises the policy even when the connecting role owns the
-- table (the scratch DB's tables are owned by the regsuper superuser running the script). Without FORCE, SET ROLE
-- to regsuper_app already makes RLS apply, but FORCE keeps the test faithful to the migration as deployed.
ALTER TABLE project      ENABLE ROW LEVEL SECURITY;  ALTER TABLE project      FORCE ROW LEVEL SECURITY;
ALTER TABLE finding      ENABLE ROW LEVEL SECURITY;  ALTER TABLE finding      FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_event  ENABLE ROW LEVEL SECURITY;  ALTER TABLE audit_event  FORCE ROW LEVEL SECURITY;
ALTER TABLE api_call     ENABLE ROW LEVEL SECURITY;  ALTER TABLE api_call     FORCE ROW LEVEL SECURITY;
ALTER TABLE handoff      ENABLE ROW LEVEL SECURITY;  ALTER TABLE handoff      FORCE ROW LEVEL SECURITY;
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
CREATE POLICY p_handoff_tenant ON handoff FOR ALL TO regsuper_app
  USING      ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' )
  WITH CHECK ( org_id = current_setting('app.current_org', true)::uuid
               OR current_setting('app.is_platform_admin', true) = 'on' );

-- ── seed two tenants (as the owner/superuser → RLS bypassed) ────────────────────────────────────────────────
INSERT INTO organization (id) VALUES
  ('11111111-1111-1111-1111-111111111111'),
  ('22222222-2222-2222-2222-222222222222');
INSERT INTO project (id, org_id) VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '22222222-2222-2222-2222-222222222222');
INSERT INTO finding (id, org_id, project_id, statement) VALUES
  ('a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1', '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'org-A secret'),
  ('b2b2b2b2-b2b2-b2b2-b2b2-b2b2b2b2b2b2', '22222222-2222-2222-2222-222222222222', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'org-B secret');

-- ════════════════════════════════════════════════════════════════════════════════════════════════════════════
-- Act as the application login. SET ROLE to a NOBYPASSRLS role activates RLS for the session.
-- ════════════════════════════════════════════════════════════════════════════════════════════════════════════
SET ROLE regsuper_app;

-- TEST 3 (run FIRST, while the GUC was never set → genuinely NULL) — a session with NO org GUC admits NOTHING
-- (the NULL-safe predicate is fail-closed, never fail-open). NB: we test the NEVER-SET state, not RESET — the
-- real app's _apply_org_guc either sets a valid UUID or leaves the GUC unset (it never writes an empty string).
DO $$
DECLARE total int;
BEGIN
  SELECT count(*) INTO total FROM finding;
  IF total <> 0 THEN RAISE EXCEPTION 'AC-RLS-4 FAIL (T3): no-GUC session sees % findings — must be 0 (fail-closed)', total; END IF;
  RAISE NOTICE 'PASS T3: a session with NO org GUC sees 0 rows (fail-closed, never fail-open)';
END $$;

-- TEST 1 — org-A session sees ONLY org-A rows, ZERO org-B leak (SELECT isolation).
SET app.current_org = '11111111-1111-1111-1111-111111111111';
DO $$
DECLARE total int; leaked int;
BEGIN
  SELECT count(*) INTO total  FROM finding;
  SELECT count(*) INTO leaked FROM finding WHERE org_id = '22222222-2222-2222-2222-222222222222';
  IF leaked <> 0 THEN RAISE EXCEPTION 'AC-RLS-4 FAIL (T1): org-A session sees % org-B finding row(s) — CROSS-TENANT LEAK', leaked; END IF;
  IF total  <> 1 THEN RAISE EXCEPTION 'AC-RLS-4 FAIL (T1): org-A session sees % findings, expected exactly 1', total; END IF;
  RAISE NOTICE 'PASS T1: org-A sees only its own 1 finding, 0 org-B leak';
END $$;

-- TEST 2 — org-B session sees ONLY org-B rows (symmetry).
SET app.current_org = '22222222-2222-2222-2222-222222222222';
DO $$
DECLARE total int; leaked int;
BEGIN
  SELECT count(*) INTO total  FROM finding;
  SELECT count(*) INTO leaked FROM finding WHERE org_id = '11111111-1111-1111-1111-111111111111';
  IF leaked <> 0 THEN RAISE EXCEPTION 'AC-RLS-4 FAIL (T2): org-B session sees % org-A row(s) — CROSS-TENANT LEAK', leaked; END IF;
  IF total  <> 1 THEN RAISE EXCEPTION 'AC-RLS-4 FAIL (T2): org-B session sees % findings, expected 1', total; END IF;
  RAISE NOTICE 'PASS T2: org-B sees only its own 1 finding, 0 org-A leak';
END $$;

-- TEST 4 — WITH CHECK blocks org-A from WRITING an org-B row (no cross-tenant write).
SET app.current_org = '11111111-1111-1111-1111-111111111111';
DO $$
BEGIN
  BEGIN
    INSERT INTO finding (id, org_id, project_id, statement)
      VALUES ('c3c3c3c3-c3c3-c3c3-c3c3-c3c3c3c3c3c3', '22222222-2222-2222-2222-222222222222', NULL, 'org-A forging org-B');
    RAISE EXCEPTION 'AC-RLS-4 FAIL (T4): org-A INSERTED an org-B row — WITH CHECK did not block a cross-tenant write';
  EXCEPTION
    WHEN insufficient_privilege THEN
      RAISE NOTICE 'PASS T4: WITH CHECK blocked org-A from writing an org-B row';
  END;
END $$;

-- TEST 5 — the platform-admin GUC sees ALL rows (the documented break-glass path).
SET app.is_platform_admin = 'on';
DO $$
DECLARE total int;
BEGIN
  SELECT count(*) INTO total FROM finding;
  IF total <> 2 THEN RAISE EXCEPTION 'AC-RLS-4 FAIL (T5): platform admin sees % findings, expected all 2', total; END IF;
  RAISE NOTICE 'PASS T5: platform-admin (is_platform_admin=on) sees all 2 findings (break-glass)';
END $$;
RESET app.is_platform_admin;

-- ════════════════════════════════════════════════════════════════════════════════════════════════════════════
-- NULL-org WRITE-REJECTION (LEDGER-M18) — the cascade/observability write paths (api_call cost rows, audit_event,
-- handoff offers) historically INSERTed with org_id=NULL (LEDGER-C3/H6). Under enforced RLS a NULL-org INSERT
-- MUST be rejected by the WITH CHECK (NULL = <uuid> is NULL, never true → fail-closed), NOT silently accepted.
-- These tests run as a NORMAL org session (valid org GUC, NOT platform-admin) so the policy is the only guard.
-- ════════════════════════════════════════════════════════════════════════════════════════════════════════════
SET app.current_org = '11111111-1111-1111-1111-111111111111';

-- TEST 6 — a NULL-org audit_event write is rejected by WITH CHECK.
DO $$
BEGIN
  BEGIN
    INSERT INTO audit_event (id, org_id, event_type)
      VALUES ('e6e6e6e6-e6e6-e6e6-e6e6-e6e6e6e6e6e6', NULL, 'cascade.l2');
    RAISE EXCEPTION 'AC-RLS-4 FAIL (T6): a NULL-org audit_event row was INSERTED — WITH CHECK did not block it';
  EXCEPTION
    WHEN insufficient_privilege THEN
      RAISE NOTICE 'PASS T6: WITH CHECK blocked a NULL-org audit_event write';
  END;
END $$;

-- TEST 7 — a NULL-org api_call (cost/token) write is rejected by WITH CHECK.
DO $$
BEGIN
  BEGIN
    INSERT INTO api_call (id, org_id, project_id)
      VALUES ('a7a7a7a7-a7a7-a7a7-a7a7-a7a7a7a7a7a7', NULL, NULL);
    RAISE EXCEPTION 'AC-RLS-4 FAIL (T7): a NULL-org api_call row was INSERTED — cost/usage tracking would silently drop';
  EXCEPTION
    WHEN insufficient_privilege THEN
      RAISE NOTICE 'PASS T7: WITH CHECK blocked a NULL-org api_call write';
  END;
END $$;

-- TEST 8 — a NULL-org handoff write is rejected by WITH CHECK.
DO $$
BEGIN
  BEGIN
    INSERT INTO handoff (id, org_id, contact_payload)
      VALUES ('80808080-8080-8080-8080-808080808080', NULL, 'orphaned PII');
    RAISE EXCEPTION 'AC-RLS-4 FAIL (T8): a NULL-org handoff row was INSERTED — tenant-invisible orphaned PII';
  EXCEPTION
    WHEN insufficient_privilege THEN
      RAISE NOTICE 'PASS T8: WITH CHECK blocked a NULL-org handoff write';
  END;
END $$;
RESET app.current_org;

RESET ROLE;
SELECT '✅ AC-RLS-4 ALL TESTS PASSED — tenant isolation enforced by the migration-015 predicate' AS result;
