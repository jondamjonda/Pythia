"""B2 — graph-changed freshness monitor (pythia.ingest.graph_changed_consumer). PURE tests, NO DB.

Pins the inert + drift contracts with a fully SYNTHETIC async pool/conn stub (no real Postgres is opened —
`get_pool` is monkeypatched, so the safety rule "never write to live pythia" holds by construction):
  • flag OFF  → {"enabled": False}, zero queries issued;
  • enabled + drift + dry_run=True (default) → computes the summary, writes NOTHING;
  • enabled + drift + dry_run=False → drains exactly the pending rows + emits exactly ONE `gnn-stale`;
  • enabled + sub-threshold churn → no consume, no emit, even when dry_run=False;
  • the threshold boundary + the no-prior-build guard (pure `_drift`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import pythia.ingest.graph_changed_consumer as gcc
from pythia.ingest.graph_changed_consumer import DRIFT_THRESHOLD, _drift, consume_graph_changed


class _FakeConn:
    """Records every execute() and captures emitted events; serves canned graph-changed rows for fetch()."""

    def __init__(self, pending_rows: list[dict]):
        self._pending = pending_rows
        self.executed: list[tuple] = []  # (sql, args) for every execute()
        self.consumed_ids: list | None = None  # ids passed to the UPDATE ... consumed_at
        self.emitted: list[tuple[str, dict]] = []  # (kind, payload) captured from the INSERT

    async def fetch(self, sql: str, *args):
        # The consumer drains the source-kind set with `WHERE kind = ANY($1::text[])`, passing
        # list(gcc._SOURCE_KINDS) — the renamed TUPLE ('graph-changed','reindex-required'). Match on the
        # ANY($1 drain SQL + that exact arg, so the stub serves the pending rows for the new tuple contract.
        if "ANY($1" in sql and args and list(args[0]) == list(gcc._SOURCE_KINDS):
            return self._pending
        return []

    async def execute(self, sql: str, *args):
        self.executed.append((sql, args))
        if "UPDATE nexus_event SET consumed_at" in sql:
            self.consumed_ids = list(args[0])
        if "INSERT INTO nexus_event" in sql:
            # _emit calls execute(INSERT, kind, payload_json) — capture it as a real emit.
            import json as _json

            self.emitted.append((args[0], _json.loads(args[1])))
        return "OK"

    # _emit also prints; nothing else needed.
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


def _row():
    return {"id": uuid4(), "payload": "{}", "created_at": datetime.now(UTC)}


class _Settings:
    def __init__(self, enabled: bool):
        self.graph_changed_monitor_enabled = enabled


def _wire(monkeypatch, *, enabled: bool, pending: list[dict], live: dict, last: dict | None):
    """Patch get_settings + get_pool + the two fingerprint primitives — no real DB, no real config."""
    conn = _FakeConn(pending)
    pool = _FakePool(conn)
    monkeypatch.setattr(gcc, "get_settings", lambda: _Settings(enabled))

    async def _get_pool():
        return pool

    async def _current(_conn):
        return live

    async def _last(_conn):
        return last

    monkeypatch.setattr(gcc, "get_pool", _get_pool)
    monkeypatch.setattr(gcc, "current_fingerprint", _current)
    monkeypatch.setattr(gcc, "last_fingerprint", _last)
    return conn


# ─────────────────────────────── inert: flag OFF ───────────────────────────────


async def test_disabled_returns_inert_and_touches_nothing(monkeypatch):
    conn = _wire(
        monkeypatch,
        enabled=False,
        pending=[_row(), _row()],
        live={"edge_count": 9999, "edge_hash": 1},
        last={"edge_count": 10, "edge_hash": 2},
    )
    out = await consume_graph_changed()
    assert out == {"enabled": False}
    assert conn.executed == []  # zero queries
    assert conn.emitted == []  # nothing emitted


# ─────────────────────────────── enabled + drift ───────────────────────────────


async def test_dry_run_computes_but_writes_nothing(monkeypatch):
    pending = [_row(), _row(), _row()]
    # +40% edge growth = well past the 10% threshold → stale, but dry_run is the DEFAULT.
    conn = _wire(
        monkeypatch,
        enabled=True,
        pending=pending,
        live={"edge_count": 1400, "edge_hash": 7},
        last={"edge_count": 1000, "edge_hash": 3},
    )
    out = await consume_graph_changed()
    assert out["enabled"] is True
    assert out["stale"] is True
    assert out["pending"] == 3
    assert out["drained"] == 0  # nothing drained on a dry run
    assert out["emitted"] is False
    assert out["dry_run"] is True
    assert out["fingerprint"] == {"edge_count": 1400, "edge_hash": 7}
    # SAFETY: a dry run must not have UPDATEd or INSERTed anything.
    assert conn.consumed_ids is None
    assert conn.emitted == []
    assert all("UPDATE" not in sql and "INSERT" not in sql for sql, _ in conn.executed)


async def test_enabled_drift_drains_and_emits_exactly_one_stale(monkeypatch):
    pending = [_row(), _row(), _row()]
    expected_ids = [r["id"] for r in pending]
    conn = _wire(
        monkeypatch,
        enabled=True,
        pending=pending,
        live={"edge_count": 1400, "edge_hash": 7},
        last={"edge_count": 1000, "edge_hash": 3},
    )
    out = await consume_graph_changed(dry_run=False)

    assert out["enabled"] is True
    assert out["stale"] is True
    assert out["emitted"] is True
    assert out["drained"] == 3
    assert out["fingerprint"] == {"edge_count": 1400, "edge_hash": 7}

    # Drained EXACTLY the pending rows (oldest-first ids), no more no less.
    assert conn.consumed_ids == expected_ids
    # Emitted EXACTLY ONE gnn-stale, carrying the live fingerprint + n drained.
    assert len(conn.emitted) == 1
    kind, payload = conn.emitted[0]
    assert kind == "gnn-stale"
    assert payload["fingerprint"] == {"edge_count": 1400, "edge_hash": 7}
    assert payload["n_drained"] == 3
    assert payload["drift"]["stale"] is True


async def test_enabled_subthreshold_churn_no_consume_no_emit(monkeypatch):
    # +1% edge growth (10→ under threshold of 10%) even though the hash moved → NOT stale → no write.
    pending = [_row()]
    conn = _wire(
        monkeypatch,
        enabled=True,
        pending=pending,
        live={"edge_count": 1010, "edge_hash": 9},
        last={"edge_count": 1000, "edge_hash": 3},
    )
    out = await consume_graph_changed(dry_run=False)
    assert out["stale"] is False
    assert out["emitted"] is False
    assert out["drained"] == 0
    assert out["drift"]["hash_changed"] is True  # hash moved …
    assert out["drift"]["reason"] == "hash_drift_subthreshold"  # … but count stayed within tolerance
    assert conn.consumed_ids is None
    assert conn.emitted == []


# ─────────────────────────────── pure _drift contract ───────────────────────────────


def test_drift_no_prior_build_is_never_stale():
    d = _drift({"edge_count": 5000, "edge_hash": 1}, None)
    assert d["stale"] is False
    assert d["reason"] == "no_prior_build"


def test_drift_threshold_boundary_is_inclusive():
    last = {"edge_count": 1000, "edge_hash": 1}
    at = _drift({"edge_count": int(1000 * (1 + DRIFT_THRESHOLD)), "edge_hash": 2}, last)
    just_under = _drift({"edge_count": int(1000 * (1 + DRIFT_THRESHOLD)) - 1, "edge_hash": 2}, last)
    assert at["stale"] is True  # exactly at threshold → stale (>=)
    assert just_under["stale"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
