-- UI Build Plan §4.4 / Phase 0 — down-sampled system-stats time-series (Tier-2 history, >1 h).
--
-- The collector keeps the last hour live in an in-memory ring (1 h at 5 s = 720 points). Every Nth tick
-- it writes ONE down-sampled scalar row here so the System panel can chart multi-hour / multi-day trends
-- without holding everything in RAM. At persist-every=12 (60 s) this is ~1440 rows/day; the collector
-- prunes rows older than 30 days, so the table stays small and the trend queries stay cheap.

BEGIN;

CREATE TABLE IF NOT EXISTS system_sample (
    ts               timestamptz NOT NULL DEFAULT now(),
    cpu_pct          real,
    mem_pct          real,
    swap_pct         real,
    disk_pct         real,
    proc_uss_bytes   bigint,   -- summed USS of the project's own processes (the app footprint)
    mps_alloc_bytes  bigint,   -- MPS allocation IF a torch process was alive this tick, else NULL
    pressure         text      -- normal | elevated | high
);

CREATE INDEX IF NOT EXISTS ix_system_sample_ts ON system_sample (ts DESC);

COMMIT;
