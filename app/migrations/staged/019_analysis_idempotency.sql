-- 019_analysis_idempotency.sql
-- PHASE C · LANE C3 (T-C3a / AS-4) — per-tenant idempotency on analysis_run.
--
-- ============================================================================
-- ⚠️  FILE-STAGED — DO NOT APPLY.  THIS MIGRATION IS NOT PART OF THE APPLIED
--     CHAIN.  Like 015/016/017/018 it lives under migrations/staged/ so the
--     migration harness (scripts/apply_migrations.py) and Postgres initdb never
--     auto-apply it (initdb does not recurse into subdirs — LEDGER-C5).  It is
--     EXCLUDED from apply_migrations.py's APPLIED_CHAIN/STAGED tuples; promote it
--     only when the async lane (analyze_async) is ready to flip (the deferred
--     AS-8 gate).  Until then it is inert: the idempotency guard in
--     analysis/tasks.py is consulted ONLY on the async-task path, and that path
--     is dark at analyze_async=False (the default).
-- ============================================================================
--
-- WHAT THIS DOES
--   Migration 011 created analysis_run.idempotency_key as a GLOBALLY-unique
--   column (`idempotency_key TEXT UNIQUE`).  That is wrong for a multi-tenant
--   product: two different orgs must be free to reuse the same client-chosen key
--   string, and a key only needs to be unique WITHIN an org (the b3-06a org_id
--   discipline — every metering write is org-predicated).  This migration:
--
--     1. DROPS the global UNIQUE(idempotency_key) constraint that 011 created
--        inline (auto-named analysis_run_idempotency_key_key).  Discovered by
--        name to be robust to naming variance; idempotent (only acts if present).
--     2. ADDS a partial composite UNIQUE index on (org_id, idempotency_key)
--        WHERE idempotency_key IS NOT NULL.  Partial so the many legacy/sync runs
--        that carry NO idempotency_key (the default path) are never forced unique
--        against each other — only keyed submits collide, and only within an org.
--
--   The unique (org_id, idempotency_key) index is the source of truth for AS-4:
--   two submits with the same key under the same org → one analysis_run row (one
--   run, one bill); different keys → two runs; the same key under two orgs → two
--   runs (correct multi-tenant behaviour).
--
-- ADDITIVE / REVERSIBLE: no table is created or dropped, no data is touched.
-- Dropping a redundant uniqueness constraint and adding a narrower one is a
-- behaviour-compatible tightening within a tenant + relaxation across tenants.
-- The rollback restores the 011 global UNIQUE.

BEGIN;

-- 1. Drop the 011 global UNIQUE(idempotency_key) (per-tenant supersedes it).
DO $$
DECLARE c_name text;
BEGIN
  SELECT con.conname INTO c_name
  FROM pg_constraint con
  JOIN pg_class rel ON rel.oid = con.conrelid
  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
  WHERE con.contype = 'u'
    AND rel.relname = 'analysis_run'
    AND att.attname = 'idempotency_key'
    AND array_length(con.conkey, 1) = 1   -- the single-column global unique only
  LIMIT 1;

  IF c_name IS NOT NULL THEN
    EXECUTE format('ALTER TABLE analysis_run DROP CONSTRAINT %I', c_name);
  END IF;
END $$;

-- 2. Add the per-tenant composite unique (partial: keyed runs only).
CREATE UNIQUE INDEX IF NOT EXISTS ux_analysis_run_org_idem
    ON analysis_run (org_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

COMMIT;


-- ===========================================================================
-- ROLLBACK — 019_analysis_idempotency.sql  →  restores the 011 global UNIQUE
-- and drops the per-tenant composite index. (Never DROP TABLE / never drop data.)
-- ===========================================================================
-- BEGIN;
--
-- DROP INDEX IF EXISTS ux_analysis_run_org_idem;
--
-- DO $$ BEGIN
--   IF NOT EXISTS (
--     SELECT 1 FROM pg_constraint con
--     JOIN pg_class rel ON rel.oid = con.conrelid
--     JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
--     WHERE con.contype = 'u' AND rel.relname = 'analysis_run'
--       AND att.attname = 'idempotency_key' AND array_length(con.conkey, 1) = 1
--   ) THEN
--     ALTER TABLE analysis_run ADD CONSTRAINT analysis_run_idempotency_key_key UNIQUE (idempotency_key);
--   END IF;
-- END $$;
--
-- COMMIT;
