"""VL-G-08 (WS3 step 27) — request-id correlation (the join key for all measurement).

The engine exposes the request id on the X-Pythia-Request-Id response header AND inside audit_trace, so a
costed api_call row (app side) can be joined to the exact engine trace. Pinned on the LLM-free /v1/retrieve
path (needs only the corpus DB).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from pythia.service import app


def test_retrieve_carries_matching_request_id_in_header_and_trace():
    with TestClient(app) as c:
        r = c.post("/v1/retrieve", json={"query": "depozitář oznamovací povinnost", "jurisdiction": ["CZ"]})
        assert r.status_code == 200
        rid = r.headers.get("X-Pythia-Request-Id")
        assert rid, "the X-Pythia-Request-Id header must be present"
        assert r.json().get("audit_trace", {}).get("request_id") == rid  # same id in header and trace


def test_distinct_requests_get_distinct_ids():
    with TestClient(app) as c:
        a = c.post("/v1/retrieve", json={"query": "lhůta", "jurisdiction": ["CZ"]}).headers["X-Pythia-Request-Id"]
        b = c.post("/v1/retrieve", json={"query": "pokuta", "jurisdiction": ["CZ"]}).headers["X-Pythia-Request-Id"]
        assert a and b and a != b
