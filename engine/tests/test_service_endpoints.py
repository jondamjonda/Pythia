"""The Pythia sidecar = the REMOTE NexusPort. The new read endpoints serialize correctly (incl. date →
ISO string, the thing a naive dataclass→JSON would 500 on), return the expected shapes, preserve order,
and never 500 on valid input. FastAPI TestClient runs the lifespan, so the asyncpg pool is warmed on
entry and released on exit.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient

from pythia.service import app


def test_health_live_is_fast_and_needs_no_lifespan():
    """ops-79 (E6): GET /health/live returns 200 WITHOUT running the lifespan — no pool
    warm, no DB, no Ollama — so it stays responsive on a busy single worker (a supervisor
    can poll it as a starvation-proof liveness signal). No context manager ⇒ no lifespan."""
    c = TestClient(app)  # no `with` → lifespan (pool warm) is NOT run
    r = c.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_health_reports_db():
    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200 and r.json()["db_connected"] is True


def test_candidates_serializes_dates_and_filters():
    with TestClient(app) as c:
        r = c.post("/v1/candidates", json={"jurisdiction": ["CZ"], "limit": 3})
        assert r.status_code == 200
        rows = r.json()
        assert rows and all(row["jurisdiction"] == "CZ" for row in rows)
        for row in rows:  # date fields are ISO strings or null — never a 500
            assert row["valid_from"] is None or isinstance(row["valid_from"], str)
            assert row["valid_to"] is None or isinstance(row["valid_to"], str)


def test_provisions_order_preserved_and_browse_shape():
    with TestClient(app) as c:
        ids = [row["id"] for row in c.post("/v1/candidates", json={"jurisdiction": ["CZ"], "limit": 4}).json()]
        got = c.post("/v1/provisions", json={"ids": ids})
        assert got.status_code == 200
        rows = got.json()
        assert [x["id"] for x in rows] == ids  # input order preserved
        assert all("analysis_roles" in x and "bindingness" in x for x in rows)
        b = c.get("/v1/browse")
        assert b.status_code == 200 and {"facets", "regimes", "spine", "catalogue"} <= set(b.json())


def test_monitoring_scans_shape_and_version_event_invariant():
    """The scan log returns its three feeds; the version-event feed only ever carries real amendments
    (version_no>1). Read-only — never 500s on a reachable DB. (Shape, not data: the corpus has no
    multi-version provisions in its pristine state — the 90/2012 § 9 amendment used to validate this end
    to end was a one-off live test that was reverted, so this asserts the contract, not a specific row.)"""
    with TestClient(app) as c:
        r = c.get("/v1/monitoring/scans")
        assert r.status_code == 200
        body = r.json()
        assert {"discovery_queue", "recent_version_events", "stale_signals"} == set(body)
        assert isinstance(body["discovery_queue"], list)  # empty if the watcher never ran (no 500)
        assert isinstance(body["recent_version_events"], list)
        assert isinstance(body["stale_signals"], list)
        # every surfaced version event is a genuine amendment: a provision_version event has version_no>1,
        # a document_version event carries a change_summary. (Empty list is valid on a pristine corpus.)
        for e in body["recent_version_events"]:
            assert e["event_type"] in ("provision_version", "document_version")
            if e["event_type"] == "provision_version":
                assert e["version_no"] is not None and e["version_no"] > 1, e


def test_monitoring_decision_404_on_unknown_queue_id():
    """A decision on a random (absent) queue id is a 404 — it records nothing and (by construction)
    triggers no ingestion."""
    with TestClient(app) as c:
        r = c.post("/v1/monitoring/decision", json={"queue_id": str(uuid4()), "decision": "approved", "note": "test"})
        assert r.status_code == 404


def test_monitoring_decision_rejects_bad_decision():
    """An invalid decision value is a 422 before any DB write is attempted."""
    with TestClient(app) as c:
        r = c.post("/v1/monitoring/decision", json={"queue_id": str(uuid4()), "decision": "ingest-now"})
        assert r.status_code == 422
