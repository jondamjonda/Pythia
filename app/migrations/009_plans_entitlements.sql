-- 009_plans_entitlements.sql
-- v5 plans / entitlements / subscription — A2-T09 · §2.4 / §16.4 AUTHORITATIVE.
--
-- Three NEW tables and EXACTLY THREE seed plan rows (lite / business / advisor).
-- `entitlement` is denormalised 1:1 with `plan` (PK = plan_id) so the
-- entitlements guard reads exactly one row. `subscription` carries the
-- point-in-time invariant (valid_to IS NULL = current) plus a partial UNIQUE
-- index (`ux_sub_one_active`) that enforces at most ONE active subscription per
-- org.
--
-- INERT GUARANTEE: nothing reads these tables until `entitlements_enabled=True`
-- (default False = pure pass-through, §config A1-T02). Seeding three plan rows
-- changes no behaviour: there are no subscriptions and no guard consumers yet.
--
-- §16.4 SCOPE RULE — Conduit/API is a SEPARATELY METERED ADD-ON, *not* a fourth
-- `plan` row. It is gated by `entitlement.api_access` (true on `advisor`, or
-- granted per-org) and billed via `usage_counter` + `billing_event
-- (event_type='conduit.call')` (011). There is therefore NO 'conduit' plan row:
-- exactly the three rows lite/business/advisor are seeded and a guard assertion
-- proves a conduit-plan query returns zero (anti-scope-creep, RDS-4/5 deleted).
--
-- PUBLIC (NO RLS): `plan` and `entitlement` are shared reference data (the price
-- book is not tenant-scoped) and stay on the §2.11 PUBLIC list — NO RLS is added
-- to them, here or in 015. `subscription` IS tenant-bearing and is on the 015
-- ENFORCED RLS list (org_id predicate added at 015, not here).
--
-- Seed UUIDs are FIXED LITERALS (no uuid_generate_v4() at migration time) so the
-- plan ids are deterministic across every environment and the entitlement rows
-- can reference them without a SELECT round-trip — same no-nondeterministic-
-- generator discipline as 007's sentinel org.
--
-- DEPENDS: 006 (organization — FK target of subscription.org_id).

BEGIN;

-- ============================================================================
-- plan  (PUBLIC reference data — NO RLS)
-- ============================================================================
CREATE TABLE plan (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code           TEXT NOT NULL UNIQUE,                  -- 'lite'|'business'|'advisor'
    name           TEXT NOT NULL,
    tier           SMALLINT NOT NULL,                     -- 1|2|3
    price_cents    INTEGER NOT NULL DEFAULT 0,
    currency       TEXT NOT NULL DEFAULT 'CZK',           -- advisor seat priced EUR (metadata)
    billing_period TEXT NOT NULL DEFAULT 'month',
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    metadata       JSONB NOT NULL DEFAULT '{}'::jsonb     -- seat_price_eur, overage rates, metered-api rates
);

-- ============================================================================
-- entitlement  (denormalised 1:1 with plan, PK = plan_id — PUBLIC, NO RLS)
-- ============================================================================
CREATE TABLE entitlement (
    plan_id            UUID PRIMARY KEY REFERENCES plan(id) ON DELETE CASCADE,
    max_depth          TEXT NOT NULL DEFAULT 'L2',          -- 'L2'|'L3'|'L4'
    answer_mode        TEXT NOT NULL DEFAULT 'information',  -- 'information'|'compliance'
    queries_per_day    INTEGER NOT NULL DEFAULT 10,         -- -1 = unlimited
    analyses_per_month INTEGER NOT NULL DEFAULT 3,          -- -1 = unlimited
    max_seats          INTEGER NOT NULL DEFAULT 1,
    audit_export       BOOLEAN NOT NULL DEFAULT FALSE,
    api_access         BOOLEAN NOT NULL DEFAULT FALSE,      -- Conduit reads this
    multi_client       BOOLEAN NOT NULL DEFAULT FALSE,
    change_alerts      BOOLEAN NOT NULL DEFAULT FALSE,
    -- The answer surface (POST /api/answer) is UNIVERSAL across all tiers:
    -- differentiation is depth + quota, not whether you may ask. DEFAULT TRUE so
    -- the require_entitlement('answer') guard passes for every tier; tiering off
    -- is possible by setting FALSE on a specific row. (Staged migration 020 adds
    -- this same column to an already-applied DB; here it is part of a fresh seed.)
    answer             BOOLEAN NOT NULL DEFAULT TRUE,
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ============================================================================
-- subscription  (tenant-bearing — RLS predicate added at 015, NOT here)
-- ============================================================================
CREATE TABLE subscription (
    id                   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id               UUID NOT NULL REFERENCES organization(id) ON DELETE CASCADE,
    plan_id              UUID NOT NULL REFERENCES plan(id),
    status               TEXT NOT NULL DEFAULT 'active',     -- 'trialing'|'active'|'past_due'|'canceled'
    seats_purchased      INTEGER NOT NULL DEFAULT 1,
    valid_from           TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to             TIMESTAMPTZ,                        -- NULL = current (point-in-time invariant)
    external_billing_ref TEXT,                              -- provider sub id; never PK
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_sub_org_current ON subscription(org_id) WHERE valid_to IS NULL;
CREATE INDEX ix_sub_status      ON subscription(status);
-- At most ONE active subscription per org (point-in-time invariant enforcement).
CREATE UNIQUE INDEX ux_sub_one_active ON subscription(org_id)
    WHERE valid_to IS NULL AND status IN ('trialing','active','past_due');

-- ============================================================================
-- SEED — the §16.4 tier model. EXACTLY THREE plan rows. NO 'conduit' row.
-- Fixed plan UUIDs (deterministic; no generator at migration time):
--   lite     = 00000000-0000-0000-0000-0000000009a1
--   business = 00000000-0000-0000-0000-0000000009a2
--   advisor  = 00000000-0000-0000-0000-0000000009a3
-- ============================================================================
INSERT INTO plan (id, code, name, tier, price_cents, currency, billing_period, is_active) VALUES
    ('00000000-0000-0000-0000-0000000009a1', 'lite',     'Lite',     1,      0, 'CZK', 'month', TRUE),
    ('00000000-0000-0000-0000-0000000009a2', 'business', 'Business', 2,      0, 'CZK', 'month', TRUE),
    ('00000000-0000-0000-0000-0000000009a3', 'advisor',  'Advisor',  3,      0, 'CZK', 'month', TRUE);

-- entitlement rows, denormalised 1:1 with the three plans (the §16.4 table):
--   code      tier max_depth answer_mode  q/day  an/mo seats export api  multi alerts answer
--   lite       1   L2        information   10     3     1     f      f    f     f      t
--   business   2   L4        compliance    200    30    3     t      f    f     t      t
--   advisor    3   L4        compliance    -1     -1    25    t      t    t     t      t
-- `answer` is TRUE on every tier — answering is universal (see CREATE TABLE note).
INSERT INTO entitlement (
    plan_id, max_depth, answer_mode, queries_per_day, analyses_per_month,
    max_seats, audit_export, api_access, multi_client, change_alerts, answer
) VALUES
    ('00000000-0000-0000-0000-0000000009a1', 'L2', 'information',  10,   3,  1, FALSE, FALSE, FALSE, FALSE, TRUE),
    ('00000000-0000-0000-0000-0000000009a2', 'L4', 'compliance',  200,  30,  3, TRUE,  FALSE, FALSE, TRUE,  TRUE),
    ('00000000-0000-0000-0000-0000000009a3', 'L4', 'compliance',   -1,  -1, 25, TRUE,  TRUE,  TRUE,  TRUE,  TRUE);

-- ============================================================================
-- Anti-scope-creep assertion (§16.4): there must be EXACTLY three plan rows and
-- NO 'conduit' plan row. Conduit is metered, never a plan SKU. Fails the
-- migration loudly if a fourth/'conduit' row was ever introduced.
-- ============================================================================
DO $$
DECLARE
    n_plans   INTEGER;
    n_conduit INTEGER;
BEGIN
    SELECT count(*) INTO n_plans   FROM plan;
    SELECT count(*) INTO n_conduit FROM plan WHERE code = 'conduit';
    IF n_plans <> 3 THEN
        RAISE EXCEPTION '009 seed invariant: expected exactly 3 plan rows, found %', n_plans;
    END IF;
    IF n_conduit <> 0 THEN
        RAISE EXCEPTION '009 seed invariant: a conduit plan row exists (Conduit is metered, NOT a plan SKU, §16.4)';
    END IF;
END
$$;

COMMIT;
