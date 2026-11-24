-- 007_tenancy_backfill.sql
-- PHASE A · A2-T07 (WP-007) — tenancy backfill + harden, INERT behind
-- tenancy_enabled=False. This migration changes NO behaviour: it only attaches a
-- single sentinel "legacy" organization to the pre-tenancy rows so the tenant FK
-- added NOT VALID in 006 can be VALIDATEd, and so the not-null invariants can be
-- set on the columns that are always tenant-scoped.
--
-- Sentinel legacy-org UUID literal (DECISIONS REGISTER D2-sentinel-org / WP-005):
--   00000000-0000-0000-0000-000000000001
-- This is a FIXED, VALID-HEX, named constant. It is INSERTed VERBATIM — no
-- uuid4() / gen_random_uuid() / any non-deterministic generator at migration
-- time. The doc-02 §2.2 string `00000000-0000-0000-0000-0000000legacy` is
-- SUPERSEDED and MUST NOT be used: it is invalid hex and fails the ::uuid cast.
-- The same sentinel org is the single-tenant / anonymous fallback that the
-- WP-022 auth layer (api/auth.py, require_auth=False) will attach no-session
-- requests to, replacing the per-request client_id=uuid4() at api/projects.py:54.
--
-- ORDER (mandated): (1) INSERT sentinel org  ->  (2) backfill org_id on
-- project/finding/audit_event/api_call  ->  (3) VALIDATE CONSTRAINT
-- fk_project_org  ->  (4) SET NOT NULL on project.org_id + finding.org_id.
-- audit_event.org_id and api_call.org_id are LEFT NULLABLE (historical / system
-- rows stay platform-only-visible; not backfilled to the sentinel).

BEGIN;

-- (1) Sentinel legacy organization — INSERTED BEFORE the FK is VALIDATEd so the
--     pre-tenancy project rows (whose org_id still holds a uuid4() client_id)
--     can be re-pointed at a real, existing organization row. Literal is the
--     WP-005 constant, verbatim; idempotent via ON CONFLICT.
INSERT INTO organization (id, name, org_type, status, metadata)
VALUES (
    '00000000-0000-0000-0000-000000000001'::uuid,
    '__legacy_pretenancy__',
    'sme',
    'active',
    '{"sentinel": true, "purpose": "pre-tenancy legacy backfill (WP-005)"}'::jsonb
)
ON CONFLICT (id) DO NOTHING;

-- (2) Backfill org_id.
--   project: every pre-tenancy row carries a uuid4() in org_id that is not a
--   real organization — re-point those (and any NULL) to the sentinel. After
--   this, every project row references an existing organization, so the FK can
--   VALIDATE.
UPDATE project
   SET org_id = '00000000-0000-0000-0000-000000000001'::uuid
 WHERE org_id IS NULL
    OR org_id NOT IN (SELECT id FROM organization);

--   finding: inherit org_id from the owning project (always present via the
--   project_id FK). Belt-and-suspenders sentinel fallback for any orphan.
UPDATE finding f
   SET org_id = p.org_id
  FROM project p
 WHERE f.project_id = p.id
   AND f.org_id IS DISTINCT FROM p.org_id;
UPDATE finding
   SET org_id = '00000000-0000-0000-0000-000000000001'::uuid
 WHERE org_id IS NULL;

--   audit_event: org_id stays NULL on ALL historical rows here. We deliberately
--   do NOT backfill it — the append-only trigger trg_audit_event_no_update
--   (migration 001) forbids UPDATE on audit_event rows (INV-5), and the column is
--   nullable by design (historical rows are platform-admin-only-visible, §2.10).
--   New audit rows receive org_id at WRITE time via the audit layer
--   (WP-022/WP-025), never by a retroactive UPDATE.

--   api_call: inherit from project where a project_id is present; system rows
--   (NULL project_id — embeddings, future Conduit) stay NULL. Left nullable.
UPDATE api_call c
   SET org_id = p.org_id
  FROM project p
 WHERE c.project_id = p.id
   AND c.org_id IS DISTINCT FROM p.org_id;

-- (3) Now that every project.org_id references an existing organization,
--     validate the tenant FK added NOT VALID in 006.
ALTER TABLE project VALIDATE CONSTRAINT fk_project_org;

-- (4) Harden the always-tenant-scoped columns. (audit_event/api_call stay
--     nullable by design.)
ALTER TABLE project ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE finding ALTER COLUMN org_id SET NOT NULL;

COMMIT;


-- ============================================================================
-- ROLLBACK (007_tenancy_backfill.sql)
-- ----------------------------------------------------------------------------
-- Destructive statements are confined to this rollback section only. Run this
-- block (uncommented) to reverse 007. This drops the NOT NULL hardening and
-- re-invalidates the FK (re-adding it NOT VALID, matching 006's post-state), and
-- clears the backfilled org_id values back to NULL. The sentinel organization
-- row is removed last. Roll back 007 BEFORE rolling back 006.
-- ============================================================================
-- BEGIN;
--
-- ALTER TABLE finding ALTER COLUMN org_id DROP NOT NULL;
-- ALTER TABLE project ALTER COLUMN org_id DROP NOT NULL;
--
-- -- Re-create the FK in its 006 (NOT VALID) shape.
-- ALTER TABLE project DROP CONSTRAINT IF EXISTS fk_project_org;
-- ALTER TABLE project ADD CONSTRAINT fk_project_org
--     FOREIGN KEY (org_id) REFERENCES organization(id) ON DELETE RESTRICT NOT VALID;
--
-- -- Clear the backfilled sentinel attributions.
-- UPDATE api_call    SET org_id = NULL WHERE org_id = '00000000-0000-0000-0000-000000000001'::uuid;
-- (audit_event.org_id is never backfilled — append-only INV-5; nothing to clear.)
-- UPDATE finding     SET org_id = NULL WHERE org_id = '00000000-0000-0000-0000-000000000001'::uuid;
-- UPDATE project     SET org_id = NULL WHERE org_id = '00000000-0000-0000-0000-000000000001'::uuid;
--
-- DELETE FROM organization WHERE id = '00000000-0000-0000-0000-000000000001'::uuid;
--
-- COMMIT;
