"""Unit tests for the AUTO-INGESTION sidecar endpoints (pythia/service.py §4.2):
  GET  /v1/ingestion/status
  POST /v1/ingestion/run-once
  GET  /v1/ingestion/history

FULLY ISOLATED — no live DB, no models, no network, no live mutation:
  • pythia.service.get_pool is monkeypatched to a fake asyncpg pool/conn (so "never touch live pythia" holds
    by construction; the FastAPI lifespan's warm/close are stubbed too);
  • the orchestrator (run_cycle / run_auto_ingest) is patched so a run-once NEVER does real work — it only
    proves the single-flight 409 vs the background-spawn 200;
  • the data/ rendezvous (the on/off mirror + the single-flight lock) points at a tmp dir.

These assert the contract SHAPES the app proxy depends on, plus the run-once single-flight behaviour:
  1. /status returns the full contract envelope; `enabled` reflects the data/ mirror file; last_run is the
     latest ingestion_run row mapped to the contract last-run shape (or null);
  2. /run-once returns 200 {started, run_id} when no lock is held, and 409 {already_running} when one is;
  3. /history returns {runs:[...]} with each row in the contract shape;
  4. _next_run_at computes the weekly/daily cron forms (and returns None on a non-trivial cron).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import pythia.service as svc
from pythia.ingest import _resource_signals as rs


# --------------------------------------------------------------------------- fakes
class _FakeConn:
    """A minimal asyncpg-conn stand-in for the ingestion read endpoints. Scripts `last_run` (the latest
    ingestion_run row), the discovery-queue backlog count, the H9/H13 metric reads, and the alarm feed."""

    def __init__(
        self,
        *,
        last_run: dict | None = None,
        history: list[dict] | None = None,
        queue_pending: int = 0,
        coverage_gap: int = 0,
        defined_terms: int = 0,
        alarms: list[str] | None = None,
    ):
        self.executed: list[str] = []
        self._last_run = last_run
        self._history = history if history is not None else ([last_run] if last_run else [])
        self._queue_pending = queue_pending
        self._coverage_gap = coverage_gap
        self._defined_terms = defined_terms
        self._alarms = alarms or []

    async def execute(self, sql: str, *args):
        self.executed.append(sql)
        return "OK"

    async def fetchval(self, sql: str, *args):
        if "discovery_queue" in sql and "pending" in sql:
            return self._queue_pending
        if "embedding_bge IS NULL" in sql:
            return self._coverage_gap
        if "count(*) FROM defined_term" in sql:
            return self._defined_terms
        return None

    async def fetchrow(self, sql: str, *args):
        if "FROM ingestion_run" in sql:
            return self._last_run
        return None

    async def fetch(self, sql: str, *args):
        if "FROM ingestion_run" in sql:
            return list(self._history)
        if "FROM nexus_event" in sql:
            return [{"kind": k} for k in self._alarms]
        return []


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
        lock_ttl_s=3600,
        cron_auto_ingest="30 4 * * 0",
        cron_discover="30 3 * * *",
        data_dir=tmp_path,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient over the sidecar app with the DB pool faked + the data/ rendezvous pointed at tmp_path.
    Returns (TestClient, conn, tmp_path). The lifespan's get_pool/close_pool are stubbed so entering the
    client never touches a real DB."""
    conn = _FakeConn()
    pool = _FakePool(conn)

    async def _get_pool():
        return pool

    async def _close_pool():
        return None

    # Reset the lazy-DDL latch so each test exercises the ensure path against the fake conn.
    monkeypatch.setattr(svc, "_INGEST_DDL_DONE", False, raising=False)
    monkeypatch.setattr(svc, "get_pool", _get_pool)
    monkeypatch.setattr(svc, "close_pool", _close_pool)
    # Both the service's settings and the resource-signals data dir resolve to tmp_path.
    monkeypatch.setattr("pythia.config.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(rs, "get_settings", lambda: _settings(tmp_path))
    # App mirror ON by default (status `enabled` reflects it).
    (tmp_path / rs.AUTO_INGEST_ENABLED_FILENAME).write_text("1", encoding="utf-8")

    with TestClient(svc.app) as c:
        yield c, conn, tmp_path


def _set_conn(client_tuple, conn):
    """Swap the fake pool's conn for a per-test one (so a test can script last_run/history/metrics)."""
    c, _old, tmp = client_tuple
    # The app holds one pool instance from the fixture; re-point get_pool at a pool over the new conn.
    pool = _FakePool(conn)

    async def _get_pool():
        return pool

    svc.get_pool = _get_pool  # patched attribute on the module the endpoints read
    return c, conn, tmp


# --------------------------------------------------------------------------- 1) /status shape
def test_status_shape_and_enabled_reflects_mirror(client, monkeypatch):
    c, _conn, tmp = client
    conn = _FakeConn(
        last_run={
            "id": uuid4(),
            "started_at": datetime(2026, 6, 29, 4, 30, tzinfo=timezone.utc),
            "finished_at": datetime(2026, 6, 29, 4, 35, tzinfo=timezone.utc),
            "mode": "auto",
            "n_discovered": 3,
            "n_ingested": 1,
            "n_amended": 1,
            "n_embedded": 12,
            "n_enriched": 2,
            "n_errors": 0,
            "halted": False,
            "halt_reason": None,
        },
        queue_pending=4,
        coverage_gap=5420,
        defined_terms=5566,
        alarms=["auto-ingest.finished"],
    )
    c, conn, tmp = _set_conn((c, _conn, tmp), conn)

    r = c.get("/v1/ingestion/status")
    assert r.status_code == 200
    body = r.json()
    # Full contract envelope present.
    assert set(body) >= {
        "enabled",
        "dry_run",
        "running",
        "last_run",
        "next_run_at",
        "queue_pending",
        "coverage_gap_in_force",
        "defined_terms_total",
        "alarms",
    }
    assert body["enabled"] is True  # the mirror file says "1"
    assert body["dry_run"] is True
    assert body["running"] is False  # no lock held
    assert body["queue_pending"] == 4
    assert body["coverage_gap_in_force"] == 5420
    assert body["defined_terms_total"] == 5566
    assert body["alarms"] == ["auto-ingest.finished"]
    # last_run is the contract last-run shape with id→run_id + ISO timestamps.
    lr = body["last_run"]
    assert set(lr) == set(svc._LAST_RUN_COLS)
    assert isinstance(lr["run_id"], str)
    assert lr["started_at"].startswith("2026-06-29T04:30")
    assert lr["mode"] == "auto" and lr["n_embedded"] == 12


def test_status_enabled_false_when_mirror_absent(client):
    c, _conn, tmp = client
    (tmp / rs.AUTO_INGEST_ENABLED_FILENAME).unlink()  # absent == OFF
    r = c.get("/v1/ingestion/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["last_run"] is None  # empty ledger → null last_run


def test_status_running_true_when_lock_held(client):
    c, _conn, tmp = client
    assert rs.acquire_lock(lock_ttl_s=3600) is True  # simulate a cycle in flight
    try:
        body = c.get("/v1/ingestion/status").json()
        assert body["running"] is True
    finally:
        rs.release_lock()


# --------------------------------------------------------------------------- 2) /run-once single-flight
def test_run_once_starts_when_idle(client, monkeypatch):
    c, _conn, tmp = client
    called: dict = {}

    async def _run_auto_ingest(*, dry_run=None):
        called["dry_run"] = dry_run
        return {"run_id": "engine-side-id", "mode": "manual", "halted": False, "halt_reason": None}

    monkeypatch.setattr("pythia.ingest.scheduler.run_auto_ingest", _run_auto_ingest)

    r = c.post("/v1/ingestion/run-once", json={"dry_run": False})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is True
    assert isinstance(body["run_id"], str) and len(body["run_id"]) >= 32  # a correlation uuid


def test_run_once_409_when_already_running(client, monkeypatch):
    c, _conn, tmp = client

    async def _boom(*, dry_run=None):
        raise AssertionError("run-once must NOT start a second cycle while the lock is held")

    monkeypatch.setattr("pythia.ingest.scheduler.run_auto_ingest", _boom)
    assert rs.acquire_lock(lock_ttl_s=3600) is True  # a run is already in flight
    try:
        r = c.post("/v1/ingestion/run-once", json={})
        assert r.status_code == 409
        assert r.json()["detail"] == "already_running"
    finally:
        rs.release_lock()


# --------------------------------------------------------------------------- 3) /history shape
def test_history_shape(client):
    c, _conn, tmp = client
    rows = [
        {
            "id": uuid4(),
            "started_at": datetime(2026, 6, 29, 4, 30, tzinfo=timezone.utc),
            "finished_at": datetime(2026, 6, 29, 4, 35, tzinfo=timezone.utc),
            "mode": "auto",
            "n_discovered": 2,
            "n_ingested": 1,
            "n_amended": 0,
            "n_embedded": 8,
            "n_enriched": 1,
            "n_errors": 0,
            "halted": False,
            "halt_reason": None,
            "defined_terms_before": 5566,
            "defined_terms_after": 5570,
            "detail": {"stages": ["discover"]},
        },
    ]
    conn = _FakeConn(history=rows)
    c, conn, tmp = _set_conn((c, _conn, tmp), conn)

    r = c.get("/v1/ingestion/history?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert "runs" in body and isinstance(body["runs"], list) and len(body["runs"]) == 1
    row = body["runs"][0]
    assert set(row) >= {
        "run_id",
        "started_at",
        "finished_at",
        "mode",
        "n_discovered",
        "n_ingested",
        "n_amended",
        "n_embedded",
        "n_enriched",
        "n_errors",
        "halted",
        "halt_reason",
        "defined_terms_before",
        "defined_terms_after",
        "detail",
    }
    assert isinstance(row["run_id"], str)
    assert row["started_at"].startswith("2026-06-29T04:30")
    assert row["defined_terms_after"] == 5570
    assert row["detail"] == {"stages": ["discover"]}


def test_history_empty_when_no_rows(client):
    c, _conn, tmp = client
    r = c.get("/v1/ingestion/history")
    assert r.status_code == 200
    assert r.json() == {"runs": []}


# --------------------------------------------------------------------------- 4) _next_run_at cron math
def test_next_run_at_weekly_sunday():
    # Default cron "30 4 * * 0" = weekly Sunday 04:30. From a Wednesday, next fire is the coming Sunday.
    now = datetime(2026, 6, 24, 12, 0)  # 2026-06-24 is a Wednesday
    nxt = svc._next_run_at(now)
    assert nxt is not None
    t = datetime.fromisoformat(nxt)
    assert t.weekday() == 6 and t.hour == 4 and t.minute == 30  # python weekday: Sunday==6
    assert t > now


def test_next_run_at_daily(monkeypatch, tmp_path):
    monkeypatch.setattr("pythia.config.get_settings", lambda: _settings(tmp_path, cron_auto_ingest="30 3 * * *"))
    now = datetime(2026, 6, 24, 12, 0)
    nxt = svc._next_run_at(now)
    t = datetime.fromisoformat(nxt)
    # Already past 03:30 today → tomorrow 03:30.
    assert t.hour == 3 and t.minute == 30 and t.date() == datetime(2026, 6, 25).date()


def test_next_run_at_none_on_complex_cron(monkeypatch, tmp_path):
    monkeypatch.setattr("pythia.config.get_settings", lambda: _settings(tmp_path, cron_auto_ingest="*/15 4 1 * *"))
    assert svc._next_run_at(datetime(2026, 6, 24, 12, 0)) is None
