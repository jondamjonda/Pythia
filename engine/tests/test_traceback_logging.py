"""AQ (WS0 step 8, E4) — unhandled /v1 errors must log a FULL traceback + request_id at ERROR.

Before this, a route raising an unhandled exception returned a bare Starlette 500 with NO traceback in the
logs (56 tracebackless 500s on 2026-07-04), making engine failures un-diagnosable. The telemetry-envelope
middleware now logs the traceback and records a 500 in the ring before re-raising. Success path unchanged.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from pythia.service import app

# A throwaway route that always raises, so the middleware's except-branch is exercised.
_MARKER = "boom-step8-marker"


@app.get("/_test/raise-unhandled")
async def _raise_unhandled():  # pragma: no cover - registered only for this test module
    raise RuntimeError(_MARKER)


def test_unhandled_error_logs_traceback(caplog):
    # raise_server_exceptions=False → the client returns the 500 instead of re-raising into the test,
    # so we can assert on what the middleware logged.
    with TestClient(app, raise_server_exceptions=False) as c:
        with caplog.at_level(logging.ERROR, logger="pythia.service"):
            r = c.get("/_test/raise-unhandled")
    assert r.status_code == 500
    recs = [rec for rec in caplog.records if "unhandled_error" in rec.getMessage()]
    assert recs, "expected an 'unhandled_error' ERROR record"
    rec = recs[0]
    assert rec.levelno == logging.ERROR
    assert rec.exc_info is not None  # a real traceback was attached
    assert "request_id=" in rec.getMessage()
    # the actual exception + its message are in the formatted traceback
    assert _MARKER in logging.Formatter().format(rec)


def test_success_path_logs_no_unhandled_error(caplog):
    with TestClient(app) as c:
        with caplog.at_level(logging.ERROR, logger="pythia.service"):
            r = c.get("/health")
    assert r.status_code == 200
    assert not [rec for rec in caplog.records if "unhandled_error" in rec.getMessage()]
