-- Day-13 · pooled relevance labeling support.
--
-- Originally retrieval_eval was one row per (query, expected_provision_id):
-- the lawyer just said "yes/no" on the top-1 retrieval. That's too narrow —
-- a real legal answer cites multiple provisions, and the lawyer needs to be
-- able to (a) mark the full top-N as relevant/partial/not-relevant and
-- (b) add provisions they know are missing from retrieval.
--
-- This migration adds the columns that make that workflow possible without
-- breaking existing rows.

BEGIN;

ALTER TABLE retrieval_eval
    ADD COLUMN IF NOT EXISTS query_group_id   UUID,
    ADD COLUMN IF NOT EXISTS rank_position    INT,
    ADD COLUMN IF NOT EXISTS added_by_lawyer  BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS label_kind       TEXT;
    -- label_kind extends `label` ('relevant' | 'not_relevant') with the
    -- third bucket the lawyer asked for: 'partial' = gives context but
    -- doesn't answer the question. We keep `label` for back-compat and
    -- write to both columns going forward.

-- Backfill: each pre-existing row becomes its own one-element group so the
-- old labeled rows stay intact in the precision-fit math.
UPDATE retrieval_eval
   SET query_group_id = id
 WHERE query_group_id IS NULL;

-- Speed the "fetch every row for this query group" join the UI does.
CREATE INDEX IF NOT EXISTS ix_re_query_group
    ON retrieval_eval (query_group_id);

CREATE INDEX IF NOT EXISTS ix_re_unlabeled_groups
    ON retrieval_eval (query_group_id)
 WHERE label IS NULL;

COMMIT;
