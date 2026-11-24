-- 018_app_role_login.sql
-- PHASE3C · PHASE A · QC Wave-2 — LEDGER-H15 — idempotent bootstrap that gives
-- the application login role `regsuper_app` its LOGIN attribute + password so a
-- CLEAN DEPLOY can actually connect as the app role.
--
-- ============================================================================
-- WHY THIS EXISTS
--   008_rls_roles.sql creates `regsuper_app` NOBYPASSRLS but deliberately leaves
--   LOGIN + password "to the operator out of band" (008:38-48). A freshly
--   CREATEd role is NOLOGIN with no password, so on a clean rebuild the app
--   cannot authenticate as `regsuper_app` even though the run scripts' DSNs
--   reference `regsuper_app:<password>`. This step closes that reproducibility
--   gap (LEDGER-H15).
--
-- ⚠️  PASSWORD IS A PLACEHOLDER — NEVER A REAL SECRET.
--   The password is supplied as the psql variable :'app_role_password'. This
--   file contains NO credential. Run it with the password injected from the
--   environment / secret store, e.g.:
--
--     psql -v app_role_password="$REGSUPER_APP_PASSWORD" -f migrations/staged/018_app_role_login.sql
--
--   For LOCAL dev the run scripts use `regsuper_app_local`; pass that:
--     psql -v app_role_password='regsuper_app_local' -f migrations/staged/018_app_role_login.sql
--
--   It is FILE-STAGED (migrations/staged/, LEDGER-C5) so neither the asyncpg
--   apply driver nor Postgres initdb runs it automatically — it needs the psql
--   `:variable` substitution the asyncpg driver does not provide, and it must be
--   invoked explicitly as a bootstrap step with the password supplied. It is
--   idempotent: re-running it just re-sets LOGIN + the (current) password.
-- ============================================================================

\set ON_ERROR_STOP on

-- Refuse to run without an explicitly supplied password (no silent default that
-- could become a weak shared secret).
\if :{?app_role_password}
\else
  \echo 'ERROR: 018 requires -v app_role_password=... (the regsuper_app password). Aborting.'
  \quit
\endif

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'regsuper_app') THEN
    -- Mirror 008: the app role is NOBYPASSRLS so RLS actually applies to it.
    CREATE ROLE regsuper_app NOBYPASSRLS;
  END IF;
END $$;

-- Idempotent: ensure the role can log in, is NOBYPASSRLS, and set its password
-- from the supplied placeholder variable. ALTER ROLE is safe to re-run.
ALTER ROLE regsuper_app LOGIN NOBYPASSRLS PASSWORD :'app_role_password';

SELECT '✅ 018: regsuper_app has LOGIN + password set (NOBYPASSRLS).' AS result;
