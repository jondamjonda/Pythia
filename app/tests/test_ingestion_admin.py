"""Auto-ingestion control surface (AUTO_INGESTION_BUILD_SPEC §4.1/§4.3/§4.4).

Pins the APP side of the contract:

  * enable/disable flip the AUTHORITATIVE persisted flag (limits.json) AND write the
    data/AUTO_INGEST_ENABLED mirror file ('1'/'0') the engine reads — the two never diverge;
  * status MERGES the app-persisted `enabled` over whatever the sidecar reports, and stays
    authoritative even when the sidecar is off/unreachable (honest envelope);
  * the routes are gated by require_control_token (401 with a token set + no header; 503 outside
    local with no token configured);
  * run-once passes a sidecar 409 (already_running) straight through; disable engages the
    kill-switch only when a run is positively in flight.

Everything is offline: the sidecar client is mocked, get_settings() is stubbed with a tmp data_dir,
and the persisted store is redirected into that tmp dir. No DB, no network, no model load.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import admin
from regsuper.observability import killswitch, limits


# --------------------------------------------------------------------------- helpers
def _settings(
    tmp: Path,
    *,
    token=None,
    environment="local",
    open_when_unset=False,
    pythia_enabled=True,
    pythia_url="http://sidecar.test",
):
    """Settings stub exposing the fields require_control_token, _pythia_on() and the persisted store
    (data_dir) all read."""
    return SimpleNamespace(
        control_panel_token=token,
        environment=environment,
        control_room_open_when_unset=open_when_unset,
        pythia_enabled=pythia_enabled,
        pythia_url=pythia_url,
        data_dir=tmp,
    )


def _client() -> TestClient:
    """Minimal app mounting just the admin router, so Depends(require_control_token) runs exactly as in
    production."""
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def _env(tmp_path):
    """Local dev (token gate open) + a tmp data_dir wired into BOTH the admin router's get_settings and
    the limits store's get_settings, so the persisted flag + the mirror file land under tmp."""
    s = _settings(tmp_path)
    with (
        patch.object(admin, "get_settings", lambda: s),
        patch.object(limits, "get_settings", lambda: s),
        patch.object(killswitch, "get_settings", lambda: s),
    ):
        yield SimpleNamespace(
            settings=s, data_dir=tmp_path, mirror=tmp_path / limits.AUTO_INGEST_MIRROR_FILENAME
        )


def _killswitch_path(tmp: Path) -> Path:
    return tmp / "KILL_SWITCH"


# --------------------------------------------------------------------------- persisted flip + mirror
def test_enable_persists_and_writes_mirror(_env):
    resp = _client().post("/admin/api/ingestion/enable")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True}
    # Authoritative store flipped true...
    assert limits.get_auto_ingest_enabled() is True
    # ...and the engine-read mirror file says '1'.
    assert _env.mirror.exists()
    assert _env.mirror.read_text(encoding="utf-8") == "1"


def test_disable_persists_and_writes_mirror(_env):
    # Start enabled, then disable. No run in flight (status mocked running=False) → no kill-switch.
    limits.set_auto_ingest_enabled(True)
    with patch.object(
        admin.pythia_client, "ingestion_status", AsyncMock(return_value={"running": False})
    ):
        resp = _client().post("/admin/api/ingestion/disable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["killswitch_engaged"] is False
    assert limits.get_auto_ingest_enabled() is False
    assert _env.mirror.read_text(encoding="utf-8") == "0"
    # Kill-switch must NOT have been engaged when nothing was running.
    assert not _killswitch_path(_env.data_dir).exists()


def test_enable_then_disable_round_trips_the_mirror(_env):
    c = _client()
    c.post("/admin/api/ingestion/enable")
    assert _env.mirror.read_text() == "1"
    with patch.object(
        admin.pythia_client, "ingestion_status", AsyncMock(return_value={"running": False})
    ):
        c.post("/admin/api/ingestion/disable")
    assert _env.mirror.read_text() == "0"
    assert limits.get_auto_ingest_enabled() is False


def test_disable_mid_run_engages_killswitch(_env):
    """Turning OFF while a run is in flight ALSO engages the kill-switch so the cycle stops cleanly."""
    limits.set_auto_ingest_enabled(True)
    with patch.object(
        admin.pythia_client, "ingestion_status", AsyncMock(return_value={"running": True})
    ):
        resp = _client().post("/admin/api/ingestion/disable")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["killswitch_engaged"] is True
    # The kill-switch file was written into the tmp data_dir.
    assert _killswitch_path(_env.data_dir).exists()


# --------------------------------------------------------------------------- status merges `enabled`
def test_status_merges_app_enabled_over_sidecar(_env):
    """App flag is authoritative for `enabled` — it overrides whatever the sidecar reports."""
    limits.set_auto_ingest_enabled(True)
    sidecar = {
        "enabled": False,
        "dry_run": True,
        "running": False,
        "queue_pending": 3,
        "defined_terms_total": 5566,
    }
    with patch.object(admin.pythia_client, "ingestion_status", AsyncMock(return_value=sidecar)):
        resp = _client().get("/admin/api/ingestion/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True  # app override wins over the sidecar's False
    assert body["reachable"] is True
    assert body["queue_pending"] == 3  # sidecar fields preserved
    assert body["defined_terms_total"] == 5566


def test_status_authoritative_enabled_when_sidecar_unreachable(_env):
    """Even with the sidecar down, `enabled` reflects the app toggle (honest envelope, reachable=False)."""
    limits.set_auto_ingest_enabled(True)
    with patch.object(
        admin.pythia_client, "ingestion_status", AsyncMock(side_effect=httpx.ConnectError("down"))
    ):
        resp = _client().get("/admin/api/ingestion/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["reachable"] is False
    assert "error" in body


def test_status_pythia_off_envelope(tmp_path):
    """PYTHIA_ENABLED off → honest {enabled(app), reachable:False} without touching the sidecar."""
    s = _settings(tmp_path, pythia_enabled=False, pythia_url=None)
    with (
        patch.object(admin, "get_settings", lambda: s),
        patch.object(limits, "get_settings", lambda: s),
    ):
        limits.set_auto_ingest_enabled(True)
        # The sidecar client must NOT be called when pythia is off.
        spy = AsyncMock(side_effect=AssertionError("sidecar reached while PYTHIA off"))
        with patch.object(admin.pythia_client, "ingestion_status", spy):
            resp = _client().get("/admin/api/ingestion/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["reachable"] is False
    spy.assert_not_called()


# --------------------------------------------------------------------------- run-once + 409 passthrough
def test_run_once_proxies_and_forwards_dry_run(_env):
    run = AsyncMock(return_value={"started": True, "run_id": "abc-123"})
    with patch.object(admin.pythia_client, "ingestion_run_once", run):
        resp = _client().post("/admin/api/ingestion/run-once", json={"dry_run": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["started"] is True
    assert body["run_id"] == "abc-123"
    assert body["reachable"] is True
    run.assert_awaited_once_with(True)


def test_run_once_409_already_running_passthrough(_env):
    """A sidecar 409 {already_running} is surfaced to the caller AS a 409, not a generic error."""
    request = httpx.Request("POST", "http://sidecar.test/v1/ingestion/run-once")
    response = httpx.Response(409, request=request, json={"already_running": True})
    err = httpx.HTTPStatusError("409", request=request, response=response)
    with patch.object(admin.pythia_client, "ingestion_run_once", AsyncMock(side_effect=err)):
        resp = _client().post("/admin/api/ingestion/run-once", json={})
    assert resp.status_code == 409


# --------------------------------------------------------------------------- stop = kill-switch
def test_stop_engages_killswitch(_env):
    resp = _client().post("/admin/api/ingestion/stop", json={})
    assert resp.status_code == 200
    assert resp.json().get("active") is True
    assert _killswitch_path(_env.data_dir).exists()


# --------------------------------------------------------------------------- history proxy
def test_history_proxies(_env):
    hist = {"runs": [{"id": "r1", "mode": "dry_run", "n_discovered": 2}]}
    with patch.object(admin.pythia_client, "ingestion_history", AsyncMock(return_value=hist)):
        resp = _client().get("/admin/api/ingestion/history?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["reachable"] is True
    assert body["runs"][0]["id"] == "r1"


# --------------------------------------------------------------------------- AUTH gating
@pytest.mark.parametrize(
    "route,method",
    [
        ("/admin/api/ingestion/status", "get"),
        ("/admin/api/ingestion/enable", "post"),
        ("/admin/api/ingestion/disable", "post"),
        ("/admin/api/ingestion/run-once", "post"),
        ("/admin/api/ingestion/stop", "post"),
        ("/admin/api/ingestion/history", "get"),
    ],
)
def test_routes_gated_token_set_missing_header_is_401(tmp_path, route, method):
    """Token configured + no X-Control-Token → 401 before the body runs (the persisted flag is never
    flipped, so a spy on set_auto_ingest_enabled must not fire)."""
    s = _settings(tmp_path, token="s3cr3t", environment="prod")
    spy = patch.object(
        limits,
        "set_auto_ingest_enabled",
        side_effect=AssertionError("flag flipped past the auth gate"),
    )
    with (
        patch.object(admin, "get_settings", lambda: s),
        patch.object(limits, "get_settings", lambda: s),
        spy,
    ):
        c = _client()
        resp = getattr(c, method)(route) if method == "get" else getattr(c, method)(route, json={})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "route,method",
    [
        ("/admin/api/ingestion/enable", "post"),
        ("/admin/api/ingestion/disable", "post"),
    ],
)
def test_routes_gated_token_unset_outside_local_is_503(tmp_path, route, method):
    """No token configured + not local → 503 fail-closed; the flag is never touched."""
    s = _settings(tmp_path, token=None, environment="prod")
    spy = patch.object(
        limits,
        "set_auto_ingest_enabled",
        side_effect=AssertionError("flag flipped past the closed gate"),
    )
    with (
        patch.object(admin, "get_settings", lambda: s),
        patch.object(limits, "get_settings", lambda: s),
        spy,
    ):
        resp = getattr(_client(), method)(route, json={})
    assert resp.status_code == 503
