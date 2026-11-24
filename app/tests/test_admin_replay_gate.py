"""WP-026 (A4-06) — the live-spend replay endpoint is fail-closed + rate-limited.

`admin_call_replay` (POST /admin/api/calls/{id}/replay) is the ONLY /admin/api route
that re-issues a live, billable OpenAI call. Before WP-026 it was un-gated and ran on
every request. This pins the three guards added by A4-06:

  1. AUTH — it now Depends(require_control_token), so without a token (outside local)
     it never runs: 503 (token unset) / 401 (token set, wrong/missing header).
  2. KILL-SWITCH — execution is disabled unless settings.replay_enabled is True. When
     False (the default / prod) it returns 403 *before* any DB read or OpenAI client,
     so the live-spend path at the get_openai() site is never reached.
  3. RATE-LIMIT — at most one replay per 60s, process-wide (429 on a repeat inside the
     window), checked before any DB / OpenAI work.

The OpenAI client is now obtained via get_openai() (the WP-021 singleton), which we
patch to a tripwire — if any gate leaks, the spy fires and the test fails. DB-free:
every gate we assert fires before session_scope() / get_openai().

asyncio_mode=auto (pyproject) → the async route is awaited directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import admin


# --------------------------------------------------------------------------- helpers
def _settings(*, replay_enabled=False, token=None, environment="local", open_when_unset=False):
    """Settings stub exposing the fields both require_control_token and the replay body read."""
    return SimpleNamespace(
        replay_enabled=replay_enabled,
        control_panel_token=token,
        environment=environment,
        control_room_open_when_unset=open_when_unset,
        openai_api_key="sk-test-dummy",
    )


@pytest.fixture
def _reset_rate_limit():
    """The 60s rate-limit uses module state; clear it so each test starts un-throttled and
    leaves no residue for the next."""
    admin._replay_last_call_ts = 0.0
    yield
    admin._replay_last_call_ts = 0.0


@pytest.fixture
def _no_spend():
    """Tripwire: replace the OpenAI client factory + DB session so reaching the live-spend path
    (or even the DB read) is a hard failure. Any gate that leaks trips one of these."""
    spend_spy = MagicMock(side_effect=AssertionError("get_openai() reached — a gate leaked!"))

    def _boom_session(*_a, **_k):
        raise AssertionError("session_scope() reached — a gate leaked before the DB read")

    with (
        patch.object(admin, "get_openai", spend_spy),
        patch.object(admin, "session_scope", _boom_session),
    ):
        yield spend_spy


def _client():
    """A minimal app mounting just the admin router, so Depends(require_control_token) runs
    exactly as in production."""
    app = FastAPI()
    app.include_router(admin.router)
    return TestClient(app, raise_server_exceptions=True)


_REPLAY_URL = f"/admin/api/calls/{uuid4()}/replay"


# --------------------------------------------------------------------------- 1. AUTH gate
def test_replay_without_token_unset_is_503(_reset_rate_limit, _no_spend):
    """No token configured + not local → the control gate 503s before the body runs."""
    s = _settings(replay_enabled=True, token=None, environment="prod")
    with patch.object(admin, "get_settings", lambda: s):
        resp = _client().post(_REPLAY_URL, json={})
    assert resp.status_code == 503
    _no_spend.assert_not_called()


def test_replay_with_token_missing_header_is_401(_reset_rate_limit, _no_spend):
    """Token configured but no X-Control-Token header → 401, spend path never reached."""
    s = _settings(replay_enabled=True, token="s3cr3t", environment="prod")
    with patch.object(admin, "get_settings", lambda: s):
        resp = _client().post(_REPLAY_URL, json={})
    assert resp.status_code == 401
    _no_spend.assert_not_called()


def test_replay_with_wrong_token_is_401(_reset_rate_limit, _no_spend):
    s = _settings(replay_enabled=True, token="s3cr3t", environment="prod")
    with patch.object(admin, "get_settings", lambda: s):
        resp = _client().post(_REPLAY_URL, json={}, headers={"X-Control-Token": "nope"})
    assert resp.status_code == 401
    _no_spend.assert_not_called()


# --------------------------------------------------------------------------- 2. KILL-SWITCH gate
def test_replay_disabled_is_403_never_reaches_spend(_reset_rate_limit, _no_spend):
    """replay_enabled=False (the default) → 403 BEFORE the DB read and BEFORE get_openai();
    the live-spend OpenAI site is never reached."""
    s = _settings(replay_enabled=False, environment="local")  # local → token gate is open
    with patch.object(admin, "get_settings", lambda: s):
        resp = _client().post(_REPLAY_URL, json={})
    assert resp.status_code == 403
    _no_spend.assert_not_called()


async def test_replay_disabled_direct_call_is_403(_reset_rate_limit, _no_spend):
    """Same kill-switch, asserted directly on the coroutine (bypassing the dependency) so the
    403 is unambiguously the body's replay_enabled guard, not the auth gate."""
    s = _settings(replay_enabled=False)
    with patch.object(admin, "get_settings", lambda: s):
        with pytest.raises(admin.HTTPException) as ei:
            await admin.admin_call_replay(call_id=uuid4(), overrides={}, _=None)
    assert ei.value.status_code == 403
    _no_spend.assert_not_called()
    # The 60s clock must NOT advance on a rejected (disabled) call.
    assert admin._replay_last_call_ts == 0.0


# --------------------------------------------------------------------------- 3. RATE-LIMIT
async def test_replay_rate_limited_within_window(_reset_rate_limit, _no_spend):
    """With replay enabled, a second call inside 60s is 429 — checked before DB / OpenAI.
    We can't let the first call reach the live OpenAI path, so we stop it at the DB read
    (404) AFTER it has stamped the rate-limit clock; the second call must then be throttled."""
    s = _settings(replay_enabled=True, environment="local")  # local → auth gate open
    with patch.object(admin, "get_settings", lambda: s):
        # First call: passes replay_enabled + rate-limit, then hits session_scope.
        # _no_spend makes session_scope raise; that proves we got PAST the rate-limit gate
        # and the clock was stamped before the DB read.
        with pytest.raises(AssertionError, match="session_scope"):
            await admin.admin_call_replay(call_id=uuid4(), overrides={}, _=None)
        assert admin._replay_last_call_ts > 0.0  # clock advanced

        # Second call, immediately: must be refused by the rate-limit before any DB / spend.
        with pytest.raises(admin.HTTPException) as ei:
            await admin.admin_call_replay(call_id=uuid4(), overrides={}, _=None)
    assert ei.value.status_code == 429
    assert "Retry-After" in ei.value.headers


async def test_rate_limit_window_is_60s(_reset_rate_limit):
    """The configured minimum interval is 60s (the spec'd cap)."""
    assert admin._REPLAY_MIN_INTERVAL_S == pytest.approx(60.0)
