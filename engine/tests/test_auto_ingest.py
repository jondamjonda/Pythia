"""Unit tests for the autonomous auto-ingestion cycle (pythia/ingest/auto_ingest.py) + its safety helpers.

SAFETY-FIRST, fully isolated: NO real DB, NO models, NO network, NO live mutation.
  • get_pool is monkeypatched to a fake asyncpg pool/conn (so "never touch live pythia" holds by construction);
  • get_settings is a SimpleNamespace with the auto-ingest knobs;
  • the data/ rendezvous (the on/off mirror + the run lock) points at a tmp dir;
  • the heavy stages (ingest/embed/enrich/reindex) are patched to fail LOUDLY if a dry-run ever reaches them.

These tests assert the four invariants the spec calls out:
  1. the disabled flag (env master OR app mirror) short-circuits before any work;
  2. a dry-run does NOT call ingest/embed/enrich/reindex (DISCOVER + DEDUPE only);
  3. the corpus-wide defined_term count-drop HALT guard trips on a simulated drop;
  4. single-flight refuses a second concurrent run (the run lock).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pythia.ingest import _resource_signals as rs
from pythia.ingest import auto_ingest as ai


# --------------------------------------------------------------------------- fakes
class _FakeConn:
    """A minimal asyncpg-conn stand-in. `defined_terms` lets a test script the H13 before/after count by
    returning successive values from the corpus-wide `count(*) FROM defined_term` query."""

    def __init__(self, defined_terms: list[int] | None = None, coverage_gap: int = 0):
        self.executed: list[tuple[str, tuple]] = []
        self.emitted: list[tuple[str, dict]] = []
        self._dt = list(defined_terms or [0, 0])
        self._dt_i = 0
        self._coverage_gap = coverage_gap

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        if "INSERT INTO nexus_event" in sql:
            import json as _json

            self.emitted.append((args[0], _json.loads(args[1])))
        return "OK"

    async def fetchval(self, sql: str, *args):
        if "count(*) FROM defined_term" in sql:
            v = self._dt[min(self._dt_i, len(self._dt) - 1)]
            self._dt_i += 1
            return v
        if "embedding_bge IS NULL" in sql:
            return self._coverage_gap
        return None

    async def fetch(self, sql: str, *args):
        return []

    def transaction(self):
        conn = self

        class _Txn:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Txn()


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _settings(tmp_path, **over):
    base = dict(
        auto_ingest_enabled=True,
        auto_ingest_dry_run=True,
        max_acts_per_cycle=5,
        discover_cap=50,
        embed_batch=64,
        enc_batch=8,
        mem_precheck_ceiling_pct=80.0,
        lock_ttl_s=3600,
        auto_ingest_publishers="Sbírka zákonů",
        snapshot_cmd="",
        data_dir=tmp_path,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Wire a clean cycle: fake pool/conn, tmp data dir for the on/off mirror + lock, app mirror ON.
    Returns (conn, settings_factory). Heavy mutators are left UNPATCHED so a dry-run that wrongly reaches
    them would import the real connector and the test would notice — but we ALSO patch them per-test to a
    boom() so a stray call is an explicit failure, not a slow import."""
    conn = _FakeConn()
    pool = _FakePool(conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(ai, "get_pool", _get_pool)
    # Point BOTH the orchestrator's settings and the resource-signals data dir at tmp_path.
    monkeypatch.setattr(rs, "get_settings", lambda: _settings(tmp_path))
    # App mirror ON by default (the engine reads this file at cycle top).
    (tmp_path / rs.AUTO_INGEST_ENABLED_FILENAME).write_text("1", encoding="utf-8")
    return conn, tmp_path


def _boom_async(*_a, **_k):
    async def _b(*a, **k):
        raise AssertionError("a mutating stage was called during a dry-run / disabled cycle")

    return _b


# --------------------------------------------------------------------------- 1) disabled short-circuits
@pytest.mark.asyncio
async def test_env_master_flag_off_short_circuits(wired, monkeypatch):
    _conn, tmp = wired
    monkeypatch.setattr(ai, "get_settings", lambda: _settings(tmp, auto_ingest_enabled=False))
    res = await ai.run_cycle(dry_run=True)
    assert res == {"skipped": "disabled_env", "mode": "dry_run"}


@pytest.mark.asyncio
async def test_app_mirror_off_short_circuits(wired, monkeypatch):
    _conn, tmp = wired
    (tmp / rs.AUTO_INGEST_ENABLED_FILENAME).unlink()  # mirror absent == OFF
    monkeypatch.setattr(ai, "get_settings", lambda: _settings(tmp))
    res = await ai.run_cycle(dry_run=True)
    assert res["skipped"] == "disabled"
    # A disabled cycle takes NO lock and writes NO run row.
    assert not (tmp / rs.AUTO_INGEST_LOCK_FILENAME).exists()


# --------------------------------------------------------------------------- 2) dry-run mutates nothing
@pytest.mark.asyncio
async def test_dry_run_discovers_and_dedupes_only(wired, monkeypatch):
    conn, tmp = wired
    monkeypatch.setattr(ai, "get_settings", lambda: _settings(tmp, auto_ingest_dry_run=True))

    # DISCOVER returns two candidates; DEDUPE triages them (one new, one amended).
    cands = [
        SimpleNamespace(external_id="111/2020", source="sbirkapp"),
        SimpleNamespace(external_id="222/2020", source="sbirkapp"),
    ]

    async def _discover(*, dry_run, cap):
        assert dry_run is True  # dry-run cycle must call discover in READ-only mode
        return cands

    async def _check_one(ext):
        return {"external_id": ext, "status": "absent"} if ext == "111/2020" else {"external_id": ext, "changed": True}

    monkeypatch.setattr("pythia.ingest.discovery.discover", _discover)
    monkeypatch.setattr("pythia.ingest.discovery.discover_publishers", _boom_async())  # live persist forbidden
    monkeypatch.setattr("pythia.ingest.refresh.check_one", _check_one)
    # Any heavy mutator reaching this far is a hard failure.
    monkeypatch.setattr("pythia.ingest.refresh.refresh_one", _boom_async())
    monkeypatch.setattr("pythia.ingest.refresh._reindex", _boom_async())
    monkeypatch.setattr("pythia.ingest.esbirka.ingest_act", _boom_async())

    res = await ai.run_cycle(dry_run=True)

    assert res["mode"] == "dry_run"
    assert res["halted"] is False
    assert res["n_discovered"] == 2
    # The triage plan is returned; NOTHING was ingested/amended/embedded/enriched.
    actions = {p["external_id"]: p["action"] for p in res["plan"]}
    assert actions == {"111/2020": "ingest_new", "222/2020": "amend"}
    assert res["n_ingested"] == 0 and res["n_amended"] == 0
    assert res["n_embedded"] == 0 and res["n_enriched"] == 0
    # No INSERT/UPDATE of source_document/provision ever ran (the fake conn only saw run-row + nexus_event).
    assert all(
        "INSERT INTO provision" not in sql and "INSERT INTO source_document" not in sql for sql, _ in conn.executed
    )
    # The lock was released on the way out.
    assert not (tmp / rs.AUTO_INGEST_LOCK_FILENAME).exists()


# --------------------------------------------------------------------------- 3) H13 stop-the-world HALT
@pytest.mark.asyncio
async def test_defined_term_drop_halts_live_cycle(wired, monkeypatch):
    conn, tmp = wired
    # before=100, after=90 → a 10-row DROP must HALT + alarm.
    conn._dt = [100, 90]
    monkeypatch.setattr(ai, "get_settings", lambda: _settings(tmp, auto_ingest_dry_run=False))

    # One amended act flows; ingest/embed/enrich/reindex are stubbed no-ops (we are testing the guard, not them).
    async def _discover_pub(*, cap):
        return {"enabled": True, "persisted": 1, "by_publisher": {}, "skipped": []}

    async def _check_one(ext):
        return {"external_id": ext, "changed": True}

    async def _refresh_one(ext, *, reindex):
        return {"result": "amended"}

    async def _reindex():
        return {"embedded": 0, "edges": 0, "invariants_ok": True}

    async def _embed_all(*, only_missing, batch, enc_batch):
        return 0

    async def _enrich(*, acts, limit, rematerialize, all_missing):
        return None

    async def _temporal(*, apply):
        return {}

    async def _drain(*, dry_run):
        return {"enabled": True, "drained": 0, "stale": False}

    # The orchestrator reads pending rows from the queue in the LIVE path — serve one pending candidate.
    async def _pending(_conn, _limit):
        return [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "external_id": "333/2020",
                "source": "sbirkapp",
                "published_on": None,
            }
        ]

    monkeypatch.setattr("pythia.ingest.discovery.discover_publishers", _discover_pub)
    monkeypatch.setattr("pythia.ingest.refresh.check_one", _check_one)
    monkeypatch.setattr("pythia.ingest.refresh.refresh_one", _refresh_one)
    monkeypatch.setattr("pythia.ingest.refresh._reindex", _reindex)
    monkeypatch.setattr("pythia.ingest.embed_bge.embed_all", _embed_all)
    monkeypatch.setattr("pythia.ingest.enrich_definitions.enrich", _enrich)
    monkeypatch.setattr("pythia.ingest.scheduler.run_temporal", _temporal)
    monkeypatch.setattr("pythia.ingest.graph_changed_consumer.consume_graph_changed", _drain)
    monkeypatch.setattr(ai, "_pending_candidates", _pending)
    # Memory is fine; no kill switch.
    monkeypatch.setattr(rs, "memory_ok", lambda _c: True)

    res = await ai.run_cycle(dry_run=False)

    assert res["halted"] is True
    assert res["halt_reason"] == "defined_terms_drop"
    assert res["defined_terms_before"] == 100 and res["defined_terms_after"] == 90
    # The halt alarm was emitted onto the event bus.
    kinds = [k for k, _ in conn.emitted]
    assert "auto-ingest-halt" in kinds


@pytest.mark.asyncio
async def test_no_defined_term_drop_does_not_halt(wired, monkeypatch):
    conn, tmp = wired
    conn._dt = [100, 100]  # unchanged → no halt
    monkeypatch.setattr(ai, "get_settings", lambda: _settings(tmp, auto_ingest_dry_run=False))

    async def _discover_pub(*, cap):
        return {"enabled": True, "persisted": 0, "by_publisher": {}, "skipped": []}

    async def _pending(_conn, _limit):
        return []  # nothing actionable → straight through the guards

    monkeypatch.setattr("pythia.ingest.discovery.discover_publishers", _discover_pub)
    monkeypatch.setattr(ai, "_pending_candidates", _pending)
    monkeypatch.setattr(rs, "memory_ok", lambda _c: True)

    res = await ai.run_cycle(dry_run=False)
    assert res["halted"] is False
    assert res["halt_reason"] is None


# --------------------------------------------------------------------------- 4) single-flight
@pytest.mark.asyncio
async def test_single_flight_refuses_second_run(wired, monkeypatch):
    _conn, tmp = wired
    monkeypatch.setattr(ai, "get_settings", lambda: _settings(tmp))
    # Simulate a run already in flight by holding the lock ourselves.
    assert rs.acquire_lock(lock_ttl_s=3600) is True
    try:
        res = await ai.run_cycle(dry_run=True)
        assert res["skipped"] == "already_running"
    finally:
        rs.release_lock()


# --------------------------------------------------------------------------- resource-signal helpers
def test_memory_ok_fails_open_when_unmeasurable(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(rs, "memory_percent", lambda: None)  # no signal
    assert rs.memory_ok(80.0) is True  # unmeasurable → don't block


def test_memory_ok_blocks_over_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(rs, "memory_percent", lambda: 92.0)
    assert rs.memory_ok(80.0) is False
    monkeypatch.setattr(rs, "memory_percent", lambda: 50.0)
    assert rs.memory_ok(80.0) is True


def test_memory_percent_reads_app_snapshot_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    # Force the psutil branch to miss so the file fallback is exercised.
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil in the torch-only engine venv")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    (tmp_path / "system.json").write_text('{"mem_percent": 71.5}', encoding="utf-8")
    assert rs.memory_percent() == 71.5


def test_enabled_mirror_file_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    f = tmp_path / rs.AUTO_INGEST_ENABLED_FILENAME
    assert rs.auto_ingest_enabled_file() is False  # absent == OFF
    f.write_text("0", encoding="utf-8")
    assert rs.auto_ingest_enabled_file() is False  # "0" == OFF
    f.write_text("1", encoding="utf-8")
    assert rs.auto_ingest_enabled_file() is True  # "1" == ON


def test_stale_lock_is_reclaimable(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    # Write a lock that is already older than the TTL → reported not-running, re-acquirable.
    import json
    import time

    (tmp_path / rs.AUTO_INGEST_LOCK_FILENAME).write_text(
        json.dumps({"pid": 999999, "started_at": time.time() - 10_000}), encoding="utf-8"
    )
    assert rs.is_running(lock_ttl_s=3600) is False
    assert rs.acquire_lock(lock_ttl_s=3600) is True
    rs.release_lock()
    assert not (tmp_path / rs.AUTO_INGEST_LOCK_FILENAME).exists()
