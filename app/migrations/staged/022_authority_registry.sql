-- 022_authority_registry.sql
-- PHASE-0 · B3 Pillar P3 — the competent-authority registry SUBSTRATE.
--
-- ============================================================================
-- ⚠️  FILE-STAGED — like 015-021 this lives under migrations/staged/ so the
--     migration harness (scripts/apply_migrations.py) and Postgres initdb never
--     auto-apply it (initdb does not recurse into subdirs — LEDGER-C5). It is
--     EXCLUDED from apply_migrations.py's APPLIED_CHAIN/STAGED tuples; the
--     operator applies it by hand against a freshly-snapshotted regsuper DB
--     (additive + reversible, see rollback).
-- ============================================================================
--
-- WHY THIS EXISTS
--   B1's practical-facts 'authority' extractor turns § text into an abstract
--   authority TYPE ("finanční úřad", "OSSZ", "KHS", "OIP", "živnostenský
--   úřad"…). That tells the reader WHICH KIND of office is competent but NOT
--   the CONCRETE office for THEIR business. B3-P3 supplies the concrete office:
--   a registry of the real FÚ/ÚzP · OSSZ/ČSSZ · KHS · OIP · živnostenský
--   directories, plus a jurisdiction map from an administrative AREA (okres /
--   ORP / kraj — the AQ-05 gazetteer grain) to the concrete authority.code.
--
--   The later answer-time join (a SIBLING owns api/answer.py — NOT touched
--   here) is: profile.municipality_code --(AQ-05 gazetteer 021)--> okres/ORP
--   /kraj area_code --(authority_jurisdiction)--> authority.code -->
--   authority row. Rendered as a labelled DIRECTORY block ("Váš úřad: FÚ pro
--   Jihomoravský kraj, ÚzP Brno-venkov"), NEVER inside chips.
--
--   Source: the official public directories — Finanční správa (FÚ/ÚzP), ČSSZ
--   (OSSZ), the KHS krajské hygienické stanice, SÚIP OIP oblastní
--   inspektoráty, and the živnostenské úřady. The concrete directory FETCH is
--   DEFERRED to a controlled operator step (see authority_registry.py header).
--
-- WHAT THIS DOES
--   1. CREATE TABLE authority — one concrete competent office.
--   2. CREATE TABLE authority_jurisdiction — maps (area_kind, area_code) → the
--      concrete authority.code, so an administrative area resolves to an office.
--
-- PUBLIC (NO RLS): both tables are shared official reference data (like the
-- `municipality` gazetteer and the `entitlement` price book) — they belong on
-- the §2.11 PUBLIC list, carry no org_id, and have no RLS policy.
--
-- ADDITIVE / REVERSIBLE: no existing table's data is modified; the rollback
-- drops the two new tables. No FK is added to any existing table.
--
-- DEPENDS: 021 (municipality gazetteer — supplies the okres/ORP/kraj area_code
--   space that authority_jurisdiction.area_code references by VALUE; no hard FK
--   so the two reference datasets stay independently loadable/reversible).

BEGIN;

-- --------------------------------------------------------------------------
-- 1. The authority registry — one concrete competent office.
--   code            — stable registry key (e.g. 'FU-JMK' / 'OSSZ-3702' /
--                     'KHS-JMK' / 'OIP-09'). PRIMARY KEY.
--   authority_type  — the shared vocab tag (authority_vocab.py) — the SAME tag
--                     B1's practical_facts 'authority' extractor emits, so the
--                     § text ('finanční úřad') joins to this registry by type.
--   name            — official office name ("Finanční úřad pro Jihomoravský
--                     kraj, Územní pracoviště Brno-venkov").
--   address         — free-text postal address (nullable).
--   databox         — datová schránka ID (nullable).
--   source_uri      — provenance: the official directory URL the row came from.
--   fetched_at      — provenance: when that directory was fetched.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS authority (
    code           text PRIMARY KEY,
    authority_type text NOT NULL,
    name           text NOT NULL,
    address        text,
    databox        text,
    source_uri     text,
    fetched_at     timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- Resolve "which concrete offices are of type X" for the answer-time join.
CREATE INDEX IF NOT EXISTS idx_authority_type ON authority (authority_type);

-- --------------------------------------------------------------------------
-- 2. The jurisdiction map — an administrative AREA → the concrete office.
--   authority_code  — FK to authority(code).
--   area_kind       — the grain of area_code: 'okres' | 'orp' | 'kraj'
--                     (the AQ-05 gazetteer hierarchy grains). CHECK-constrained.
--   area_code       — the okres/ORP/kraj code (by VALUE from the AQ-05
--                     gazetteer; no hard FK — the two datasets load
--                     independently). NOT a code from `authority`.
--
--   A single office can cover many areas (one row per area) and an area may
--   map to several offices of DIFFERENT types (FÚ + OSSZ + KHS…), so the PK is
--   the (authority_code, area_kind, area_code) triple — idempotent upsert key.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS authority_jurisdiction (
    authority_code text NOT NULL REFERENCES authority (code) ON DELETE CASCADE,
    area_kind      text NOT NULL CHECK (area_kind IN ('okres', 'orp', 'kraj')),
    area_code      text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (authority_code, area_kind, area_code)
);

-- Forward lookup: given a resolved (area_kind, area_code), find the offices.
CREATE INDEX IF NOT EXISTS idx_authority_jurisdiction_area
    ON authority_jurisdiction (area_kind, area_code);

COMMIT;


-- ===========================================================================
-- ROLLBACK — 022_authority_registry.sql
--   Drops the two new tables (jurisdiction first — it FKs authority).
--   (Never DROP other data.)
-- ===========================================================================
-- BEGIN;
--
-- DROP TABLE IF EXISTS authority_jurisdiction;
-- DROP TABLE IF EXISTS authority;
--
-- COMMIT;
