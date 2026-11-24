-- pythia_edge — the one derived table Pythia owns (v2 §3.5). Nodes stay in `provision`.
-- Idempotent: safe to run repeatedly. Requires the `vector` extension (already enabled in the Polarius DB).

CREATE TABLE IF NOT EXISTS pythia_edge (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  src          uuid NOT NULL,                       -- provision.id (head)
  dst          uuid,                                -- provision.id (tail); NULL => dangling external ref
  dst_external text,                                -- cross_reference.to_external_ref for unresolved citations
  rel_id       smallint NOT NULL,                   -- FK into the relation registry R (relations.py)
  edge_type    text NOT NULL,                       -- human-readable relation label (mirrors rel_id)
  weight       real NOT NULL DEFAULT 1.0,           -- w₀ prior × confidence (ranking/decay; GNN-learnable, v2 §4.4)
  confidence   real,                                -- extraction confidence (1.0 = lawyer/structural)
  source       text NOT NULL,                       -- cross_reference | hierarchy | defined_term | transposition | layerB | lawyer
  deontic      smallint DEFAULT 0,                  -- endpoint deontic role (v2 §4.4 L3)
  valid_from   date,                                -- denormalized temporal window (v2 §4.10); NULL until populated
  valid_to     date,
  memory_vec   vector(64),                          -- ReMindRAG edge memory (v2 §5.3); NULL until first use
  metadata     jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Logical uniqueness on (src, rel, dst|external) — an expression index (PK can't span expressions).
CREATE UNIQUE INDEX IF NOT EXISTS uq_pe_edge ON pythia_edge
  (src, rel_id, COALESCE(dst, '00000000-0000-0000-0000-000000000000'::uuid), COALESCE(dst_external, ''));

CREATE INDEX IF NOT EXISTS ix_pe_src   ON pythia_edge (src);
-- Serves the four-corners fan-out cap's per-node `ORDER BY weight DESC, dst LIMIT N` (cte_expand): with
-- this composite Postgres walks the index for a given src and STOPS after N rows — no full out-edge
-- fetch + heapsort per lateral invocation. (ix_pe_src above is a redundant prefix but kept for clarity.)
CREATE INDEX IF NOT EXISTS ix_pe_src_weight ON pythia_edge (src, weight DESC, dst);
CREATE INDEX IF NOT EXISTS ix_pe_dst   ON pythia_edge (dst);
CREATE INDEX IF NOT EXISTS ix_pe_relid ON pythia_edge (rel_id);
CREATE INDEX IF NOT EXISTS ix_pe_valid ON pythia_edge (valid_to) WHERE valid_to IS NOT NULL;
-- memory_vec HNSW index is deferred to Phase 4 (pointless while all-NULL).

-- N4-B1b transposition map (CZ act <-> EU directive CELEX); populated by ingest/transposition.py,
-- read by the transposes/transposed_by inserts in materialize.py. Created here so materialize is
-- self-sufficient (empty table -> 0 transposition edges, never an error) even before the linker runs.
CREATE TABLE IF NOT EXISTS transposition_map (
    cz_external_id text NOT NULL,
    eu_celex       text NOT NULL,
    has_cue        boolean NOT NULL DEFAULT false,
    has_cites      boolean NOT NULL DEFAULT false,
    has_sparql     boolean NOT NULL DEFAULT false,
    notified       date,
    verified       boolean NOT NULL DEFAULT false,
    note           text,
    provenance     jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (cz_external_id, eu_celex)
);
