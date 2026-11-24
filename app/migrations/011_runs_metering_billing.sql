-- 011_runs_metering_billing.sql
-- PHASE3C · A2-T11 (the linchpin for AC-COST-MODEL + AC-ASYNC) — doc-02 §2.5/§2.6/§2.7,
-- reconciled by §16.1 (AUTHORITATIVE) and PHASE3C A1 decisions register.
--
-- Adds the merged run/metering/billing spine, INERT (additive only, no behaviour change):
--   1. analysis_run  — THE MERGED TABLE: RDS-3's analysis_run + RDS-7's analysis_job are
--      COLLAPSED into one row carrying BOTH job lifecycle AND per-run cost/metering.
--      There is deliberately NO analysis_job table (RDS-7 folded in — see assertion below).
--      Seam columns for Phase B threading (as_of / tier / requested_depth) ship nullable NOW
--      so Phase B needs no second migration.
--   2. usage_counter — pre-aggregated quota counters (cheap atomic read+increment).
--   3. billing_event — THE SINGLE append-only billing table. Both the product billing path
--      AND the Conduit module write here (event_type='conduit.call'); there is NO
--      Conduit-local billing_event. Append-only via trg_billing_event_no_update.
--   4. api_call gains analysis_run_id (FK, cost linkage) and cost_usd is WIDENED
--      NUMERIC(10,6) -> NUMERIC(12,6) to match analysis_run/usage_counter/billing_event.
--      The rollback restores NUMERIC(10,6) (NOT REAL).
--
-- INERT: nothing reads or writes these surfaces until the Phase-B flags flip
-- (analyze_async=False, billing_provider=noop by default). FKs resolve against
-- organization (migr 007), project (migr 001), business_profile (migr 010), api_call (migr 002).

BEGIN;

-- ============================================================================
-- assert NO analysis_job table exists (RDS-7 merged into analysis_run — §16.1)
-- ============================================================================
-- If a stray analysis_job table is ever introduced, this migration is the wrong
-- shape and MUST fail loudly rather than create a parallel lifecycle surface.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'analysis_job'
    ) THEN
        RAISE EXCEPTION
          'analysis_job must NOT exist: RDS-7 is merged into analysis_run (doc-02 §16.1)';
    END IF;
END $$;


-- ============================================================================
-- analysis_run  — THE MERGED TABLE (metering + job lifecycle) — doc-02 §2.5 / §16.1
-- ============================================================================
CREATE TABLE analysis_run (
    id                  BIGSERIAL PRIMARY KEY,                       -- §16.1
    org_id              UUID NOT NULL REFERENCES organization(id),   -- §16.1 (RLS enforced in 015)
    project_id          UUID REFERENCES project(id) ON DELETE SET NULL,              -- §16.1
    business_profile_id UUID REFERENCES business_profile(id) ON DELETE SET NULL,     -- §16.1
    tier                TEXT,                                        -- §16.1 (plan_code snapshot at run time) — Phase-B seam
    requested_depth     TEXT CHECK (requested_depth IN ('L2','L3','L4')),  -- §16.1 — Phase-B seam
    requested_layers    TEXT[] NOT NULL DEFAULT '{}',                -- what caller asked for
    executed_layers     TEXT[] NOT NULL DEFAULT '{}',                -- after depth clamp (e.g. {L2} for lite)
    status              TEXT CHECK (status IN ('queued','running','done','failed','refused'))
                          NOT NULL DEFAULT 'queued',                 -- §16.1
    idempotency_key     TEXT UNIQUE,                                 -- §16.1 (loud 409, no dup runs)
    cost_usd            NUMERIC(12,6) NOT NULL DEFAULT 0,            -- §16.1 (SUM of this run's api_call rows)
    tokens_input        BIGINT NOT NULL DEFAULT 0,
    tokens_output       BIGINT NOT NULL DEFAULT 0,
    error               TEXT,                                        -- §16.1
    answer_mode         TEXT NOT NULL DEFAULT 'information',         -- input-gate verdict snapshot (RDS-2)
    worker_id           TEXT,                                        -- Celery worker hostname
    as_of               DATE,                                        -- inert temporal seam (G2; threading Phase B)
    started_at          TIMESTAMPTZ,                                 -- §16.1
    finished_at         TIMESTAMPTZ,                                 -- §16.1
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()           -- §16.1
);
CREATE INDEX ix_run_org_time ON analysis_run(org_id, created_at DESC);
CREATE INDEX ix_run_status   ON analysis_run(status, created_at);
CREATE INDEX ix_run_depth    ON analysis_run(requested_depth);

-- cost linkage: per-run cost = SUM(api_call.cost_usd WHERE analysis_run_id = :id)
ALTER TABLE api_call ADD COLUMN analysis_run_id BIGINT REFERENCES analysis_run(id) ON DELETE SET NULL;
CREATE INDEX ix_api_call_run ON api_call(analysis_run_id);

-- Widen api_call.cost_usd NUMERIC(10,6) -> NUMERIC(12,6) so per-call cost shares the
-- precision of the run/counter/billing money columns. Additive (widening) — no row loss.
-- (The ORM mirror also moves REAL -> Numeric(12,6); see integrator notes.)
ALTER TABLE api_call ALTER COLUMN cost_usd TYPE NUMERIC(12,6);


-- ============================================================================
-- usage_counter  — pre-aggregated quota counters — doc-02 §2.6 (§16.5 RLS in 015)
-- ============================================================================
CREATE TABLE usage_counter (
    org_id       UUID NOT NULL REFERENCES organization(id),
    metric       TEXT NOT NULL,        -- 'queries'|'analyses'|'analyses_L4'|'api_calls'
    period_kind  TEXT NOT NULL,        -- 'day'|'month'
    period_start DATE NOT NULL,        -- UTC bucket start
    count        BIGINT NOT NULL DEFAULT 0,
    cost_usd     NUMERIC(12,6) NOT NULL DEFAULT 0,
    PRIMARY KEY (org_id, metric, period_kind, period_start)
);
-- atomic UPSERT increment (app side, INERT until Phase B):
-- INSERT ... VALUES (:org,:metric,:pk,:ps,1,:cost)
-- ON CONFLICT (org_id,metric,period_kind,period_start)
-- DO UPDATE SET count = usage_counter.count + 1, cost_usd = usage_counter.cost_usd + EXCLUDED.cost_usd;


-- ============================================================================
-- billing_event  — THE SINGLE append-only table — doc-02 §2.7 / §16.1
-- ============================================================================
-- Both the product billing path AND the Conduit module write here.
-- event_type CHECK list is governed by §16.1 and INCLUDES 'conduit.call'.
CREATE TABLE billing_event (
    id              BIGSERIAL PRIMARY KEY,
    org_id          UUID NOT NULL REFERENCES organization(id),
    event_type      TEXT NOT NULL CHECK (event_type IN
                       ('analysis.metered','quota.exceeded','seat.added',
                        'seat.removed','subscription.changed','conduit.call')),
    analysis_run_id BIGINT REFERENCES analysis_run(id),          -- nullable
    conduit_call_id UUID,                                        -- nullable (Conduit attribution)
    amount_usd      NUMERIC(12,6),
    currency        TEXT,                                        -- EUR for advisor seats; CZK for subs
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_be_org_time ON billing_event(org_id, created_at);
CREATE INDEX ix_be_type     ON billing_event(event_type);

-- Append-only at the DB level (clone of audit_event_no_modify, migr 001).
-- Corrections are new rows — never UPDATE/DELETE.
CREATE OR REPLACE FUNCTION billing_event_no_modify() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'billing_event is append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER trg_billing_event_no_update
    BEFORE UPDATE OR DELETE ON billing_event
    FOR EACH ROW EXECUTE FUNCTION billing_event_no_modify();


COMMIT;

-- ----------------------------------------------------------------------------
-- ROLLBACK — see 011_runs_metering_billing_rollback.sql
-- (separate runnable file; restores api_call.cost_usd to NUMERIC(10,6), NOT REAL,
--  and drops the three new tables + the no-modify trigger/function).
-- ----------------------------------------------------------------------------
