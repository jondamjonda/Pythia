"""WP-026 (A4-05) — the control-room gate fails CLOSED when its token is unset.

`require_control_token` is the shared dependency behind every /admin/api control
endpoint. Before WP-026 an unset CONTROL_PANEL_TOKEN meant "no gate, pass everyone
through" — fine on a loopback dev box, a silent wide-open admin surface anywhere
else. This pins the new behaviour:

  * token unset + environment != 'local'  → 503 (no pass-through), unless the
    operator explicitly opted into an open control room.
  * token unset + environment == 'local'  → OPEN (today's dev behaviour, unchanged).
  * token set                              → constant-time compare (secrets.compare_digest):
    right token passes, wrong/missing token → 401.

All DB-free / no app: the dependency is a plain function reading get_settings(),
so we call it directly with a stubbed Settings. asyncio_mode=auto is irrelevant
here — require_control_token is synchronous.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from regsuper.api import admin


# --------------------------------------------------------------------------- helpers
def _settings(*, token, environment="local", open_when_unset=False):
    """Minimal Settings stub exposing only the three fields require_control_token reads."""
    return SimpleNamespace(
        control_panel_token=token,
        environment=environment,
        control_room_open_when_unset=open_when_unset,
    )


def _call(settings, header):
    """Run the dependency with the given Settings stub + X-Control-Token header value."""
    with patch.object(admin, "get_settings", lambda: settings):
        return admin.require_control_token(x_control_token=header)


# --------------------------------------------------------------------------- token UNSET
@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_token_unset_outside_local_is_503_no_passthrough(environment):
    """The core fail-closed pin: no token + not local → 503, regardless of any header sent."""
    with pytest.raises(HTTPException) as ei:
        _call(_settings(token=None, environment=environment), header=None)
    assert ei.value.status_code == 503
    # Even an attacker-supplied header must NOT slip through when no token is configured.
    with pytest.raises(HTTPException) as ei2:
        _call(_settings(token=None, environment=environment), header="anything")
    assert ei2.value.status_code == 503


def test_token_unset_local_dev_stays_open():
    """Local dev with no token configured stays OPEN — unchanged developer experience."""
    assert _call(_settings(token=None, environment="local"), header=None) is None
    # A stray header is harmless when local + unset (still open, no 401).
    assert _call(_settings(token=None, environment="local"), header="whatever") is None


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_token_unset_open_when_unset_opt_in_reopens(environment):
    """Operator escape hatch: control_room_open_when_unset=True reopens the gate even outside local."""
    s = _settings(token=None, environment=environment, open_when_unset=True)
    assert _call(s, header=None) is None


# --------------------------------------------------------------------------- token SET
def test_token_set_correct_passes():
    s = _settings(token="s3cr3t-token", environment="prod")
    assert _call(s, header="s3cr3t-token") is None


@pytest.mark.parametrize("environment", ["local", "staging", "prod"])
def test_token_set_wrong_is_401(environment):
    s = _settings(token="s3cr3t-token", environment=environment)
    with pytest.raises(HTTPException) as ei:
        _call(s, header="wrong-token")
    assert ei.value.status_code == 401


def test_token_set_missing_header_is_401():
    """A configured token + no header is a 401 (must present the token), not a silent open."""
    s = _settings(token="s3cr3t-token", environment="local")
    with pytest.raises(HTTPException) as ei:
        _call(s, header=None)
    assert ei.value.status_code == 401


# --------------------------------------------------------------------------- constant-time compare
def test_uses_constant_time_compare_digest():
    """The token comparison must be constant-time: assert the source uses secrets.compare_digest
    (a plain `==` leaks the token byte-by-byte via timing) and never the bare `!=` it replaced."""
    src = inspect.getsource(admin.require_control_token)
    assert "secrets.compare_digest" in src
    assert "x_control_token != tok" not in src


def test_compare_digest_actually_invoked_on_set_token():
    """Behavioural proof the constant-time path runs: a configured token routes through
    secrets.compare_digest (we spy on it) rather than a short-circuiting `==`."""
    s = _settings(token="s3cr3t-token", environment="prod")
    with patch.object(admin.secrets, "compare_digest", wraps=admin.secrets.compare_digest) as spy:
        _call(s, header="s3cr3t-token")
    spy.assert_called_once_with("s3cr3t-token", "s3cr3t-token")
