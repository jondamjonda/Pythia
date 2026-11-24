-- 011_runs_metering_billing_rollback.sql
-- Exact reversal of 011_runs_metering_billing.sql. Reversible: 011 is additive, so
-- rolling back loses only the new (inert, unused) surfaces — no pre-011 data is touched.
--
-- CRITICAL down-detail (A2-T11): api_call.cost_usd is restored to NUMERIC(10,6) — the
-- migr-002 type that 011 widened — and NOT to REAL. (The ORM REAL->Numeric mirror is a
-- separate code change; this SQL only reverses the on-disk type.)

BEGIN;

-- ---- billing_event (and its append-only guard) ----------------------------
DROP TRIGGER IF EXISTS trg_billing_event_no_update ON billing_event;
DROP FUNCTION IF EXISTS billing_event_no_modify();
DROP TABLE IF EXISTS billing_event;

-- ---- usage_counter --------------------------------------------------------
DROP TABLE IF EXISTS usage_counter;

-- ---- api_call linkage + cost-width revert ---------------------------------
-- Drop the FK column first (it references analysis_run), then restore the prior
-- on-disk type. NUMERIC(12,6) -> NUMERIC(10,6) is a narrowing of the integer-digit
-- budget only (scale 6 unchanged); inert column carries no out-of-range data.
DROP INDEX IF EXISTS ix_api_call_run;
ALTER TABLE api_call DROP COLUMN IF EXISTS analysis_run_id;
ALTER TABLE api_call ALTER COLUMN cost_usd TYPE NUMERIC(10,6);

-- ---- analysis_run (the merged table) --------------------------------------
DROP TABLE IF EXISTS analysis_run;

COMMIT;
