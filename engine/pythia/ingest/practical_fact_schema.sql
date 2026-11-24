-- B3 Pillar P1 — the practical_fact SUBSTRATE (additive; not applied by main; orchestrator applies with a snapshot).
--
-- ONE additive derived table Pythia owns: verbatim, span-anchored practical facts (deadlines / authorities /
-- penalties / forms / channels / fees / procedure steps) mined OFFLINE out of a provision's own stored text.
-- It is the durable, queryable materialisation of the pure `reasoning/practical_facts.py` extractor + a later
-- cheap-LLM lhůt* sweep. V-FACT (contract C12, sibling) reads it via `reasoning/fact_lookup.py` as its single
-- source of truth, falling back to a live regex when a provision has no rows yet.
--
-- CARDINAL INVARIANT (enforced at INSERT time in enrich_practical.py AND re-asserted by practical_check.py):
--   value_raw MUST equal provision.text[char_start:char_end]  — the fact is an EXACT slice of the provision's
--   own text. No transformation, no date computation, no amount normalisation touches value_raw. value_norm
--   (jsonb) MAY hold a derived/parsed shape, but it is advisory and never the grounding.
--
-- ADDITIVE-ONLY: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS. Applying this over an existing
-- corpus adds one empty table and its indexes; it touches no existing row and no existing table, so the
-- served engine is byte-identical until rows are written (and V-FACT is default-OFF regardless).

CREATE TABLE IF NOT EXISTS practical_fact (
  id                 uuid        NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
  provision_id       uuid        NOT NULL,               -- provision.id (the app owns provision; FK omitted so
                                                         -- this stays a purely-additive Pythia-side table that
                                                         -- never blocks an app-side provision delete/re-ingest)
  fact_type          text        NOT NULL,               -- one of the 7 canonical types (CHECK below)
  value_raw          text        NOT NULL,               -- VERBATIM substring = provision.text[char_start:char_end]
  char_start         integer     NOT NULL,               -- inclusive offset into provision.text
  char_end           integer     NOT NULL,               -- exclusive offset into provision.text
  value_norm         jsonb,                              -- advisory derived shape (parsed days / amount / …); NULL ok
  extraction_method  text        NOT NULL,               -- 'regex' | 'llm'
  extractor_version  text        NOT NULL,               -- e.g. 'regex-b3p1-v1' / 'llm-lhut-v1' (provenance + re-run)
  created_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT ck_pf_fact_type CHECK (
    fact_type IN ('deadline','authority','penalty','form','channel','fee','procedure_step')),
  CONSTRAINT ck_pf_method    CHECK (extraction_method IN ('regex','llm')),
  CONSTRAINT ck_pf_span      CHECK (char_start >= 0 AND char_end > char_start)
);

-- V-FACT's hot path: lookup(provision_id[, fact_type]). Cover the (provision_id, fact_type) prefix.
CREATE INDEX IF NOT EXISTS ix_pf_provision      ON practical_fact (provision_id);
CREATE INDEX IF NOT EXISTS ix_pf_provision_type ON practical_fact (provision_id, fact_type);

-- Idempotent re-run guard: one row per (provision, type, span, extractor_version). Re-running the SAME
-- extractor version over the same provision is a no-op (ON CONFLICT DO NOTHING in enrich_practical.py);
-- a NEW extractor_version writes fresh rows without clobbering the old provenance.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pf_span_ver
  ON practical_fact (provision_id, fact_type, char_start, char_end, extractor_version);
