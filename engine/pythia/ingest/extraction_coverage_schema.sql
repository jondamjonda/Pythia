-- VL-F-15 — the extraction_coverage MANIFEST (additive; not applied by main; orchestrator applies with a snapshot).
--
-- One additive table recording WHICH (act × extractor wave × extractor_version) actually RAN. Its purpose is
-- to let reasoning/fact_lookup.py tell an UNCOVERED act ('extraction_not_run' — a silent fallback, no gap
-- event) apart from a genuinely fact-less provision on a covered act ('no_fact_row'). Without it, an act the
-- extractor never swept reports a FALSE 'no_fact_row' gap.
--
-- ADDITIVE-ONLY: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS. Applying this over an existing
-- corpus adds one empty table and its index; it touches no existing row/table. Nothing on the served path
-- reads it until fact_lookup's coverage classification is wired (default-OFF / sibling-owned), so the served
-- engine is byte-identical.
--
-- *** LIVE APPLY + THE 7-WAVE BACKFILL ARE A DEFERRED OPERATOR STEP. *** This schema + the writer in
-- pythia/ingest/extraction_coverage.py are the driver; the orchestrator applies the DDL and runs the backfill
-- (enrich_practical wave runner → record_wave per act) against the snapshot it controls, NOT here.

CREATE TABLE IF NOT EXISTS extraction_coverage (
  id                  uuid        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  external_id         text        NOT NULL,           -- the act (source_document.external_id) the wave ran over
  wave                text        NOT NULL,           -- 'regex' | 'llm'
  extractor_version   text        NOT NULL,           -- provenance (e.g. 'regex-b3p1-v1' / 'llm-lhut-v1')
  provisions_scanned  integer     NOT NULL DEFAULT 0, -- provisions of this act the wave scanned
  facts_written       integer     NOT NULL DEFAULT 0, -- facts the wave wrote for this act (observability)
  ran_at              timestamptz NOT NULL DEFAULT now(),

  -- one manifest row per (act, wave, extractor_version); the writer upserts on this key so a re-run refreshes
  -- the counts in place (a NEW extractor_version writes a fresh row without clobbering the old provenance).
  CONSTRAINT uq_ec_act_wave_ver UNIQUE (external_id, wave, extractor_version)
);

-- fact_lookup's coverage probe: "is this act in the manifest?" — an external_id lookup.
CREATE INDEX IF NOT EXISTS ix_ec_external_id ON extraction_coverage (external_id);
