-- 014_corpus_cutover.sql
-- PHASE3C · PHASE A · A2-T14 — corpus-cutover provenance/anchor fields + the
-- regsuper↔nexus id bridge.  (doc-02 §2.10, RDS-5.)
--
-- ADDITIVE + INERT.  This migration changes NO behaviour:
--   * the three new provision columns default to FALSE / 'polarius-ingest', so
--     every existing row keeps its current meaning (is_anchor_only=FALSE means
--     "substantive provision, retrieved normally" — the L1 retriever only
--     special-cases anchors once cutover begins);
--   * provision_id_map is created EMPTY.  Population is Stage-3 (WP-S3-02) and
--     is GATED behind law_source / cutover_domains flags that ship OFF
--     (law_source=regsuper, cutover_domains=[]).  F-1 (the id-mapping
--     methodology) is OUT OF SCOPE here and is NOT pulled forward — the table
--     exists so the crosswalk has somewhere to land, nothing more.
--
-- provision_id_map is PUBLIC reference data (the law is public, retrieval is
-- cross-tenant by design): it gets NO row-level security, exactly like
-- provision / source_document / cross_reference (doc-02 §2.11 PUBLIC list).
--
-- 014 IS THE LAST APPLIED ADDITIVE MIGRATION.  015 (RLS-enforce) and 016
-- (conduit_credential) are authored but FILE-STAGED — the harness must NOT
-- apply them; see their header comments.

BEGIN;

-- ---------------------------------------------------------------------------
-- AMEND provision — anchor flag + point-in-time provenance (idempotent).
-- ---------------------------------------------------------------------------
ALTER TABLE provision ADD COLUMN IF NOT EXISTS is_anchor_only         BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE provision ADD COLUMN IF NOT EXISTS valid_from_approximate BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE provision ADD COLUMN IF NOT EXISTS ingested_by            TEXT DEFAULT 'polarius-ingest';

-- Anchor rows are retrieved only via hierarchical expansion; let the L1 path
-- skip them cheaply on substantive-only scans once cutover starts.
CREATE INDEX IF NOT EXISTS ix_p_anchor_only ON provision(is_anchor_only) WHERE is_anchor_only;

-- ---------------------------------------------------------------------------
-- provision_id_map — regsuper↔nexus id bridge for the cutover (PUBLIC, NO RLS).
-- Created EMPTY; population deferred to Stage-3 WP-S3-02.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provision_id_map (
    regsuper_id  UUID NOT NULL REFERENCES provision(id) ON DELETE CASCADE,
    nexus_id     UUID NOT NULL,
    nexus_corpus TEXT NOT NULL DEFAULT 'pythia',
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (regsuper_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_provision_id_map_nexus
    ON provision_id_map(nexus_id, nexus_corpus);

COMMIT;


-- ===========================================================================
-- ROLLBACK — 014_corpus_cutover.sql
-- (destructive statements live ONLY in this rollback section; run by hand.)
-- ===========================================================================
-- BEGIN;
--
-- DROP INDEX IF EXISTS ux_provision_id_map_nexus;
-- DROP TABLE IF EXISTS provision_id_map;
--
-- DROP INDEX IF EXISTS ix_p_anchor_only;
-- ALTER TABLE provision DROP COLUMN IF EXISTS ingested_by;
-- ALTER TABLE provision DROP COLUMN IF EXISTS valid_from_approximate;
-- ALTER TABLE provision DROP COLUMN IF EXISTS is_anchor_only;
--
-- COMMIT;
