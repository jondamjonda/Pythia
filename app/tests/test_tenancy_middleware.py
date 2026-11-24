"""WP-024 (T24) — TenancyMiddleware contextvar binding.

Proves the SAFE-DEFAULT LAW for the request hot path:

  * tenancy_enabled=False (the local default) ⇒ PURE pass-through: get_principal
    is NEVER invoked and the tenancy contextvars stay at their defaults
    (current_org_var=None, is_platform_admin_var=False). Byte-identical to today.
  * tenancy_enabled=True ⇒ the principal's org is published onto current_org_var
    (as str) and is_platform_admin onto is_platform_admin_var FOR THE DURATION of
    the handler (asserted from inside call_next), then RESET to the on-entry
    defaults after the request so nothing leaks to the next request on a reused
    worker.

These tests are DB-free: we drive ``TenancyMiddleware.dispatch`` directly with a
fake ``call_next`` and mock ``get_principal`` so no database is touched. The flag
is flipped on the cached Settings instance exactly as T23's test does.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID

import pytest

from regsuper.api import middleware
from regsuper.config import get_settings
from regsuper.storage.database import current_org_var, is_platform_admin_var


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _FakePrincipal:
    """Minimal stand-in for auth.Principal — only the fields T24 reads."""

    org_id: UUID
    is_platform_admin: bool = False


def _fake_request() -> SimpleNamespace:
    """A request object exposing only what dispatch touches: ``state``.

    TenancyMiddleware passes the request straight to (mocked) get_principal and
    stashes the principal on request.state — a SimpleNamespace satisfies both.
    """
    return SimpleNamespace(state=SimpleNamespace())


def _set_tenancy_enabled(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Force get_settings().tenancy_enabled to ``value`` for one test.

    middleware.py reads the flag via get_settings() at dispatch time, so we
    patch the attribute on the cached Settings instance (same trick as T23).
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "tenancy_enabled", value, raising=False)


# ---------------------------------------------------------------------------
# 1. SAFE DEFAULT — tenancy_enabled=False ⇒ pass-through, principal not resolved.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_off_is_pass_through_and_never_resolves_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, False)

    # get_principal must NOT be called on the OFF path. Wire a tripwire that
    # raises if it ever runs (it is imported lazily inside dispatch, so patch
    # it on the auth module).
    from regsuper.api import auth

    async def _boom(*_a, **_k):  # pragma: no cover - must never execute
        raise AssertionError("get_principal must not be called when tenancy is OFF")

    monkeypatch.setattr(auth, "get_principal", _boom)

    sentinel = object()

    async def call_next(_request):
        # During the handler the contextvars are untouched at their defaults.
        assert current_org_var.get() is None
        assert is_platform_admin_var.get() is False
        return sentinel

    mw = middleware.TenancyMiddleware(app=lambda *a, **k: None)
    result = await mw.dispatch(_fake_request(), call_next)

    assert result is sentinel
    # Defaults preserved after the request — byte-identical to today.
    assert current_org_var.get() is None
    assert is_platform_admin_var.get() is False


# ---------------------------------------------------------------------------
# 2. ENABLED ⇒ org bound during the handler, reset to default afterwards.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_on_binds_org_during_handler_and_resets_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, True)

    org = UUID("22222222-2222-2222-2222-222222222222")
    principal = _FakePrincipal(org_id=org, is_platform_admin=False)

    from regsuper.api import auth

    async def _fake_get_principal(_request):
        return principal

    monkeypatch.setattr(auth, "get_principal", _fake_get_principal)

    seen: dict = {}

    async def call_next(request):
        # Inside the handler the GUC source must be populated as a STRING.
        seen["org"] = current_org_var.get()
        seen["admin"] = is_platform_admin_var.get()
        seen["state_principal"] = request.state.principal
        return SimpleNamespace(status_code=200)

    mw = middleware.TenancyMiddleware(app=lambda *a, **k: None)
    await mw.dispatch(_fake_request(), call_next)

    # During the handler: contextvar holds str(org_id), admin flag mirrored.
    assert seen["org"] == str(org)
    assert seen["admin"] is False
    # The principal is stashed on request.state for T25 to reuse (no re-resolve).
    assert seen["state_principal"] is principal

    # After the request: reset to the on-entry defaults — no leak to next request.
    assert current_org_var.get() is None
    assert is_platform_admin_var.get() is False


# ---------------------------------------------------------------------------
# 3. ENABLED + platform admin ⇒ is_platform_admin_var bound True for the handler.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_on_binds_platform_admin_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, True)

    org = UUID("33333333-3333-3333-3333-333333333333")
    principal = _FakePrincipal(org_id=org, is_platform_admin=True)

    from regsuper.api import auth

    async def _fake_get_principal(_request):
        return principal

    monkeypatch.setattr(auth, "get_principal", _fake_get_principal)

    seen: dict = {}

    async def call_next(_request):
        seen["admin"] = is_platform_admin_var.get()
        return SimpleNamespace(status_code=200)

    mw = middleware.TenancyMiddleware(app=lambda *a, **k: None)
    await mw.dispatch(_fake_request(), call_next)

    assert seen["admin"] is True
    # Reset after the request.
    assert is_platform_admin_var.get() is False


# ---------------------------------------------------------------------------
# 4. ENABLED + handler RAISES ⇒ contextvars still reset (finally branch).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_on_resets_contextvars_even_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, True)

    principal = _FakePrincipal(
        org_id=UUID("44444444-4444-4444-4444-444444444444"),
        is_platform_admin=True,
    )

    from regsuper.api import auth

    async def _fake_get_principal(_request):
        return principal

    monkeypatch.setattr(auth, "get_principal", _fake_get_principal)

    class _Boom(RuntimeError):
        pass

    async def call_next(_request):
        raise _Boom("handler exploded")

    mw = middleware.TenancyMiddleware(app=lambda *a, **k: None)
    with pytest.raises(_Boom):
        await mw.dispatch(_fake_request(), call_next)

    # The finally must have reset both vars despite the exception.
    assert current_org_var.get() is None
    assert is_platform_admin_var.get() is False
