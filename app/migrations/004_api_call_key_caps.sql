-- UI Build Plan §5.1 / Phase 1 — per-API-key attribution for weekly budget caps.
--
-- A key is identified by provider:sha256(secret)[:12] (the secret is never stored — see keys.py).
-- Pre-existing rows have no key, so they backfill to a single `legacy:<provider>` line (shown in the UI
-- as "historical, pre-key-tracking" so the operator isn't surprised by one giant legacy budget bar).

BEGIN;

ALTER TABLE api_call ADD COLUMN IF NOT EXISTS api_key_id TEXT;
UPDATE api_call SET api_key_id = 'legacy:' || provider WHERE api_key_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_api_call_ts_key ON api_call (ts, api_key_id);
CREATE INDEX IF NOT EXISTS ix_api_call_key_provider ON api_call (api_key_id, provider);

COMMIT;
