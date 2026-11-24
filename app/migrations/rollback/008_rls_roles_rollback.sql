-- 008_rls_roles_rollback.sql
-- Reverses 008_rls_roles.sql exactly: drops the four permissive policies, turns
-- RLS back OFF on the four tenant tables, revokes the grants, and drops the
-- regsuper_app role. After this runs the schema is byte-for-byte the post-007
-- state (no RLS anywhere, no app login role).
--
-- This is the ONLY place 008 's objects are dropped (DROP POLICY / DISABLE RLS /
-- DROP ROLE live exclusively inside this rollback).

BEGIN;

-- Drop the permissive policies (IF EXISTS so a partial-apply rollback is safe).
DROP POLICY IF EXISTS p_project_permissive     ON project;
DROP POLICY IF EXISTS p_finding_permissive     ON finding;
DROP POLICY IF EXISTS p_audit_event_permissive ON audit_event;
DROP POLICY IF EXISTS p_api_call_permissive    ON api_call;

-- Turn RLS back OFF on the four tenant tables.
ALTER TABLE project     DISABLE ROW LEVEL SECURITY;
ALTER TABLE finding     DISABLE ROW LEVEL SECURITY;
ALTER TABLE audit_event DISABLE ROW LEVEL SECURITY;
ALTER TABLE api_call    DISABLE ROW LEVEL SECURITY;

-- Revoke the grants, then drop the app login role.
REVOKE ALL ON project       FROM regsuper_app;
REVOKE ALL ON finding       FROM regsuper_app;
REVOKE ALL ON audit_event   FROM regsuper_app;
REVOKE ALL ON api_call      FROM regsuper_app;
REVOKE ALL ON SEQUENCE audit_event_id_seq FROM regsuper_app;

-- Drop the app login role. `regsuper_app` is a CLUSTER-GLOBAL object (roles are
-- shared across all databases in the Postgres cluster), so DROP ROLE fails if it
-- is still referenced by privileges/policies in ANOTHER database on the same
-- cluster. The harness applies one DB per cluster, so this normally succeeds;
-- the guard below degrades to a NOTICE instead of aborting the rollback if a
-- co-located DB still pins the role, so this migration's own objects are still
-- fully reverted in THIS database.
DO $$
BEGIN
    DROP ROLE IF EXISTS regsuper_app;
EXCEPTION WHEN dependent_objects_still_exist THEN
    RAISE NOTICE 'regsuper_app not dropped: still referenced by another database on this cluster; this DB''s 008 objects are reverted regardless.';
END
$$;

COMMIT;
