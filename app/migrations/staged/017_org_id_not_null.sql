-- 017_org_id_not_null.sql
-- PHASE3C · PHASE A · QC Wave-2 — LEDGER-M16 — tighten tenant ownership on the
-- RLS-enforced tables whose org_id is still NULLABLE, and stop a deleted org
-- from orphaning tenant PII on the handoff path.
--
-- ============================================================================
-- ⚠️  FILE-STAGED — DO NOT APPLY.  THIS MIGRATION IS NOT PART OF THE APPLIED
--     CHAIN.  Like 015/016 it lives under migrations/staged/ so the migration
--     harness (scripts/apply_migrations.py) and Postgres initdb never
--     auto-apply it (initdb does not recurse into subdirs — LEDGER-C5).  It is
--     EXCLUDED from apply_migrations.py's APPLIED_CHAIN; add it to STAGED only
--     when it is ready to flip.
--
--     ORDERING / PRECONDITION: this migration must be applied ONLY AFTER the
--     code that writes business_profile / advisor / handoff rows always stamps
--     a non-NULL org_id (the LEDGER-H6 fix — thread org_id into the cascade /
--     handoff writers).  Applied before that fix, the SET NOT NULL would reject
--     the very writes those paths make today, AND the backfill below assumes no
--     legitimate NULL-org rows remain.  Today there are 0 affected rows
--     (LEDGER-M16: app code already stamps org_id; the schema merely PERMITS
--     NULL), so this hardens the schema to match the invariant the code holds.
--
--     It also pairs with 015 §0.3 FORCE ROW LEVEL SECURITY (LEDGER-C1): NOT NULL
--     is the column-level guard, the RLS WITH CHECK is the row-level guard, and
--     FORCE makes both bind the owner too.
-- ============================================================================
--
-- WHAT THIS DOES
--   1. business_profile.org_id  → NOT NULL   (010:37, was nullable; ON DELETE CASCADE kept)
--   2. advisor.org_id           → NOT NULL   (010:94, was nullable; ON DELETE CASCADE kept)
--   3. handoff.org_id           → NOT NULL   (012:69, was nullable)
--   4. handoff.org_id FK        : ON DELETE SET NULL  →  ON DELETE RESTRICT
--        Rationale: handoff.contact_payload can carry consented tenant PII.
--        SET NULL on org delete would orphan that PII into a tenant-invisible
--        row (no org → no RLS tenant can ever see or erase it → a GDPR hazard).
--        RESTRICT refuses to delete an org that still has handoff rows, forcing
--        an explicit cleanup decision.  (CASCADE — delete the PII with the org —
--        is the alternative; RESTRICT is chosen so PII is never silently dropped
--        without an operator decision.  Revisit per the data-retention policy.)
--
-- business_profile.org_id and advisor.org_id already use ON DELETE CASCADE
-- (010), which is correct for tenant-owned rows — deleting the org deletes them.
-- They need only the NOT NULL tightening, not an FK change.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. Defensive backfill (should be a no-op today — app stamps org_id already).
--    Runs as the migration OWNER (BYPASSRLS), so it sees every row. If any
--    legitimately-NULL row exists, the SET NOT NULL below will RAISE and the
--    transaction rolls back — loud, by design (do not invent an org_id).
-- ---------------------------------------------------------------------------
-- business_profile / advisor / handoff: no safe source to derive a NULL org_id
-- from (they ARE the tenant root), so we do NOT backfill — we rely on the
-- precondition above (0 NULL rows). A NULL row here is a real bug to surface.

-- ---------------------------------------------------------------------------
-- 1-3. NOT NULL tightening. Each is idempotent (SET NOT NULL on an already
--      NOT NULL column is a no-op).
-- ---------------------------------------------------------------------------
ALTER TABLE business_profile ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE advisor          ALTER COLUMN org_id SET NOT NULL;
ALTER TABLE handoff          ALTER COLUMN org_id SET NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. handoff.org_id FK: SET NULL → RESTRICT. The 012 FK was created inline and
--    is auto-named `handoff_org_id_fkey`; drop by discovered name to be robust
--    to any naming variance, then recreate as RESTRICT. Idempotent: only acts
--    if a SET-NULL-ish FK is present.
-- ---------------------------------------------------------------------------
DO $$
DECLARE fk_name text;
BEGIN
  SELECT con.conname INTO fk_name
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
  WHERE con.contype = 'f'
    AND rel.relname = 'handoff'
    AND att.attname = 'org_id'
  LIMIT 1;

  IF fk_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE handoff DROP CONSTRAINT %I', fk_name);
  END IF;

  -- Recreate with ON DELETE RESTRICT (idempotent name; only created if absent).
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'handoff_org_id_fkey_restrict'
  ) THEN
    ALTER TABLE handoff
      ADD CONSTRAINT handoff_org_id_fkey_restrict
      FOREIGN KEY (org_id) REFERENCES organization(id) ON DELETE RESTRICT;
  END IF;
END $$;

COMMIT;


-- ===========================================================================
-- ROLLBACK — 017_org_id_not_null.sql  →  restores the pre-017 nullability +
-- the handoff SET NULL FK. (Drops NOT NULL; swaps the handoff FK back to
-- ON DELETE SET NULL. Never DROP TABLE / never drop data.)
-- ===========================================================================
-- BEGIN;
--
-- ALTER TABLE business_profile ALTER COLUMN org_id DROP NOT NULL;
-- ALTER TABLE advisor          ALTER COLUMN org_id DROP NOT NULL;
-- ALTER TABLE handoff          ALTER COLUMN org_id DROP NOT NULL;
--
-- DO $$ BEGIN
--   IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'handoff_org_id_fkey_restrict') THEN
--     ALTER TABLE handoff DROP CONSTRAINT handoff_org_id_fkey_restrict;
--   END IF;
--   IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'handoff_org_id_fkey') THEN
--     ALTER TABLE handoff
--       ADD CONSTRAINT handoff_org_id_fkey
--       FOREIGN KEY (org_id) REFERENCES organization(id) ON DELETE SET NULL;
--   END IF;
-- END $$;
--
-- COMMIT;
