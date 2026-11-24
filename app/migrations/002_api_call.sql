-- 002_api_call.sql
-- Platform observability: per-call cost tracking + replay log.
-- Every LLM / embedding API request is logged here so the dashboard can show
-- today's and this month's spend, and so individual calls can be inspected
-- and re-run with tweaked inputs.

BEGIN;

CREATE TABLE api_call (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider        TEXT NOT NULL,                     -- openai / anthropic / cohere
    model           TEXT NOT NULL,                     -- gpt-4o-mini, text-embedding-3-large, ...
    endpoint        TEXT NOT NULL,                     -- chat.completions / embeddings / messages
    source          TEXT NOT NULL,                     -- caller tag: definitions / embed / analysis-L2 / ...
    project_id      UUID REFERENCES project(id) ON DELETE SET NULL,

    tokens_input    INT  NOT NULL DEFAULT 0,
    tokens_output   INT  NOT NULL DEFAULT 0,
    tokens_cached   INT  NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0,

    latency_ms      INT  NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ok',        -- ok / error
    error           TEXT,

    request_json    JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_summary TEXT
);

CREATE INDEX ix_ac_ts        ON api_call(ts DESC);
CREATE INDEX ix_ac_provider  ON api_call(provider, model);
CREATE INDEX ix_ac_source    ON api_call(source, ts DESC);
CREATE INDEX ix_ac_project   ON api_call(project_id, ts DESC);
CREATE INDEX ix_ac_status    ON api_call(status) WHERE status <> 'ok';

COMMIT;
