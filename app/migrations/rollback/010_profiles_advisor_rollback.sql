-- 010_profiles_advisor_rollback.sql
-- Reverses 010_profiles_advisor.sql exactly. Drops the FK column off project,
-- then advisor_client, advisor, business_profile (FK-dependency order:
-- advisor_client references both advisor and business_profile; project.
-- business_profile_id references business_profile), then the trigger function.
-- After this runs the schema is the post-009 state.
--
-- This is the ONLY place 010's objects are dropped.

BEGIN;

-- Drop the project FK column + its partial index first (it references
-- business_profile, which is dropped below).
DROP INDEX IF EXISTS ix_proj_bp;
ALTER TABLE project DROP COLUMN IF EXISTS business_profile_id;

-- Drop the advisor workspace tables (child first).
DROP TABLE IF EXISTS advisor_client;
DROP TABLE IF EXISTS advisor;

-- Drop business_profile and its updated_at trigger + function.
DROP TRIGGER IF EXISTS trg_bp_updated_at ON business_profile;
DROP TABLE IF EXISTS business_profile;
DROP FUNCTION IF EXISTS bp_set_updated_at();

COMMIT;
