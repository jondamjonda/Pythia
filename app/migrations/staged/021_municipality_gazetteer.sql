-- 021_municipality_gazetteer.sql
-- PHASE-0 · AQ-05 — the shared official municipality gazetteer.
--
-- ============================================================================
-- ⚠️  FILE-STAGED — like 015-020 this lives under migrations/staged/ so the
--     migration harness (scripts/apply_migrations.py) and Postgres initdb never
--     auto-apply it (initdb does not recurse into subdirs — LEDGER-C5). It is
--     EXCLUDED from apply_migrations.py's APPLIED_CHAIN/STAGED tuples; the
--     operator applies it by hand. For AQ-05 it was applied by hand against the
--     freshly-snapshotted regsuper DB (additive + reversible, see rollback).
-- ============================================================================
--
-- WHY THIS EXISTS
--   AQ-05 needs ONE shared, authoritative table mapping the Czech RÚIAN/ČSÚ
--   6-digit obec code (kód obce) to its administrative hierarchy:
--     okres (LAU1 / CZ-NUTS4) · ORP (obec s rozšířenou působností) · kraj
--     (NUTS3). This gazetteer is consumed later by Track A's region slot and
--     Track B's office-registry join; today it also validates the municipality
--     code exposed on business_profile (below).
--   Source: ČSÚ číselník 43 (CISOB) + its vazby to 65 (SO ORP), 101 (okres) and
--   100 (kraj), plus 101/100 for the LAU1 / NUTS3 codes. ~6,254 municipalities.
--
-- WHAT THIS DOES
--   1. CREATE TABLE municipality — the gazetteer (code PK + hierarchy columns).
--   2. ADD COLUMN municipality_code to business_profile — an ADDITIVE nullable
--      column with an FK to municipality(code). The pre-existing free-text
--      registered_municipality column is KEPT untouched for back-compat.
--
-- PUBLIC (NO RLS): `municipality` is shared official reference data (like the
-- `entitlement` price book) — it belongs on the §2.11 PUBLIC list, carries no
-- org_id, and has no RLS policy. Adding an FK column to business_profile is a
-- pure additive change; the profile's own RLS is unaffected.
--
-- ADDITIVE / REVERSIBLE: no existing table's data is modified; the rollback
-- drops the FK column and the table.
--
-- DEPENDS: 010 (business_profile).

BEGIN;

-- --------------------------------------------------------------------------
-- 1. The gazetteer table.
--   code       — RÚIAN/ČSÚ 6-digit kód obce (PRIMARY KEY).
--   name       — official obec name.
--   okres_code — LAU1 / CZ-NUTS4 code of the okres (e.g. CZ0323).
--   okres_name — okres name (e.g. Plzeň-město).
--   orp_code   — ČSÚ SO ORP code (e.g. 3209).
--   orp_name   — ORP name (e.g. Plzeň).
--   kraj_code  — NUTS3 code of the kraj (e.g. CZ032).
--   kraj_name  — kraj name (e.g. Plzeňský kraj).
--   okres_csu_code / kraj_csu_code — the ČSÚ internal číselník codes (101/100),
--                kept as provenance for the vazba join; not the public LAU/NUTS.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS municipality (
    code           text PRIMARY KEY,
    name           text NOT NULL,
    okres_code     text,
    okres_name     text,
    orp_code       text,
    orp_name       text,
    kraj_code      text,
    kraj_name      text,
    okres_csu_code text,
    kraj_csu_code  text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Lookup indexes for the later region-slot / office-registry joins.
CREATE INDEX IF NOT EXISTS idx_municipality_okres_code ON municipality (okres_code);
CREATE INDEX IF NOT EXISTS idx_municipality_kraj_code  ON municipality (kraj_code);
CREATE INDEX IF NOT EXISTS idx_municipality_orp_code   ON municipality (orp_code);

-- --------------------------------------------------------------------------
-- 2. Expose the validated code on business_profile (additive, nullable, FK).
--   The existing free-text `registered_municipality` stays for back-compat.
-- --------------------------------------------------------------------------
ALTER TABLE business_profile
    ADD COLUMN IF NOT EXISTS municipality_code text
        REFERENCES municipality (code);

COMMIT;


-- ===========================================================================
-- ROLLBACK — 021_municipality_gazetteer.sql
--   Drops the FK column then the table. (Never DROP other data.)
-- ===========================================================================
-- BEGIN;
--
-- ALTER TABLE business_profile DROP COLUMN IF EXISTS municipality_code;
-- DROP TABLE IF EXISTS municipality;
--
-- COMMIT;
