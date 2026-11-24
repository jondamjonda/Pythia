-- 020_entitlement_answer_feature.sql
-- PHASE C · LANE C2 (T-C2a / answer surface) — add the `answer` boolean feature
-- column to the entitlement table.
--
-- ============================================================================
-- ⚠️  FILE-STAGED — DO NOT APPLY.  THIS MIGRATION IS NOT PART OF THE APPLIED
--     CHAIN.  Like 015/016/017/018/019 it lives under migrations/staged/ so the
--     migration harness (scripts/apply_migrations.py) and Postgres initdb never
--     auto-apply it (initdb does not recurse into subdirs — LEDGER-C5).  It is
--     EXCLUDED from apply_migrations.py's APPLIED_CHAIN/STAGED tuples; the
--     operator applies it by hand alongside the C2 entitlements flip.  Until then
--     it is inert: the entitlements guard is a pure pass-through while
--     entitlements_enabled=False (the default), so the column is consulted only
--     once that flag is turned ON.
-- ============================================================================
--
-- WHY THIS EXISTS
--   The answer endpoint (src/regsuper/api/answer.py) guards on
--   require_entitlement('answer').  The 009 entitlement table has NO `answer`
--   column, so when entitlements_enabled flips ON the guard does
--   getattr(ent, 'answer') -> None -> raises 403 feature.unknown for EVERY tier.
--   But answering is UNIVERSAL across all tiers (Lite/Business/Advisor): the
--   product differentiates on depth + quota, not on whether you may ask a
--   question at all.  This migration adds an `answer` BOOLEAN feature column
--   defaulting TRUE so the guard PASSES for every tier (and future tiering
--   remains possible by setting it FALSE on a specific tier's entitlement row).
--
-- WHAT THIS DOES
--   ADD COLUMN IF NOT EXISTS answer BOOLEAN NOT NULL DEFAULT TRUE.
--   Because the column DEFAULTs TRUE, all existing rows (the 3 seeded tiers
--   lite/business/advisor) get answer=TRUE automatically on apply — no data
--   backfill statement is needed.
--
-- PUBLIC (NO RLS): `entitlement` is shared reference data (the price book) and
-- stays on the §2.11 PUBLIC list — no RLS interaction, so adding a column is a
-- pure additive change.
--
-- ADDITIVE / REVERSIBLE: no table is created or dropped, no existing data is
-- modified destructively.  The rollback drops the column.
--
-- DEPENDS: 009 (the entitlement table).

BEGIN;

ALTER TABLE entitlement ADD COLUMN IF NOT EXISTS answer BOOLEAN NOT NULL DEFAULT TRUE;

COMMIT;


-- ===========================================================================
-- ROLLBACK — 020_entitlement_answer_feature.sql  →  drops the `answer` column.
-- (Never DROP TABLE / never drop other data.)
-- ===========================================================================
-- BEGIN;
--
-- ALTER TABLE entitlement DROP COLUMN IF EXISTS answer;
--
-- COMMIT;
