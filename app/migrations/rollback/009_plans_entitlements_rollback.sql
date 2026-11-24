-- 009_plans_entitlements_rollback.sql
-- Reverses 009_plans_entitlements.sql exactly. Drops subscription, entitlement,
-- plan (in FK-dependency order: subscription and entitlement both reference
-- plan, so they go first). The seed rows disappear with their tables. After this
-- runs the schema is the post-008 state.
--
-- This is the ONLY place 009's tables are dropped.

BEGIN;

DROP TABLE IF EXISTS subscription;
DROP TABLE IF EXISTS entitlement;
DROP TABLE IF EXISTS plan;

COMMIT;
