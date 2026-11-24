"""WP-025 (T25, G9) — audit-tenancy rewrite.

Proves the SAFE-DEFAULT LAW for the audit write on the request hot path:

  * ``log_event`` gains a keyword-only ``org_id`` parameter. When provided it is
    stamped onto the constructed AuditEvent.org_id; when omitted it is None ⇒ the
    column stays NULL exactly as today (back-compat). INSERT only — the
    append-only contract (trg_audit_event_no_update blocks UPDATE/DELETE) is
    never touched.
  * AuditMiddleware passes org_id=None when tenancy_enabled=False (the local
    default) ⇒ byte-identical to today; it reads current_org_var only when
    tenancy is ON.
  * The reversibility switch ``_AUDIT_FIRE_AND_FORGET`` ships False ⇒ the
    http.request row is AWAITED synchronously (lands before the request
    completes), with zero timing change vs today.

These tests are DB-free: ``log_event``'s session.add is exercised against a
mocked session (no engine), and AuditMiddleware.dispatch is driven directly with
a fake request / call_next while ``session_scope`` and ``log_event`` are mocked,
so no database is touched. The flag is flipped on the cached Settings instance
exactly as T23/T24's tests do.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from regsuper.api import middleware
from regsuper.config import get_settings
from regsuper.storage import audit as audit_mod
from regsuper.storage.audit import log_event
from regsuper.storage.database import current_org_var
from regsuper.storage.models import AuditEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _set_tenancy_enabled(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Force get_settings().tenancy_enabled to ``value`` for one test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "tenancy_enabled", value, raising=False)


def _fake_request(path: str = "/v1/analyze") -> SimpleNamespace:
    """A request exposing only what AuditMiddleware.dispatch touches."""
    return SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path=path),
        client=SimpleNamespace(host="127.0.0.1"),
        state=SimpleNamespace(),
    )


class _CapturingSession:
    """Stand-in for AsyncSession — captures the row passed to session.add."""

    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)


# ---------------------------------------------------------------------------
# 1. log_event accepts org_id and stamps it onto the AuditEvent row.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_log_event_stamps_org_id() -> None:
    session = _CapturingSession()
    org = UUID("55555555-5555-5555-5555-555555555555")

    await log_event(
        session,  # type: ignore[arg-type]
        actor_type="system",
        actor_id="http",
        action="http.request",
        target_type="endpoint",
        target_id="POST /v1/analyze",
        payload={"status": 200},
        org_id=org,
    )

    assert len(session.added) == 1
    evt = session.added[0]
    assert isinstance(evt, AuditEvent)
    assert evt.org_id == org


# ---------------------------------------------------------------------------
# 2. Back-compat: omitting org_id ⇒ NULL (None) exactly as today.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_log_event_org_id_defaults_to_none() -> None:
    session = _CapturingSession()

    await log_event(
        session,  # type: ignore[arg-type]
        actor_type="system",
        actor_id="http",
        action="http.request",
        target_type="endpoint",
        target_id="POST /v1/analyze",
        payload={"status": 200},
    )

    assert len(session.added) == 1
    evt = session.added[0]
    assert isinstance(evt, AuditEvent)
    assert evt.org_id is None


# ---------------------------------------------------------------------------
# 3. log_event is INSERT only — it does not commit, and never issues
#    UPDATE/DELETE (the append-only contract stays untouched).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_log_event_is_insert_only_no_commit() -> None:
    # A mock session would expose .commit/.execute/.delete if log_event called
    # them. It must only call .add(). MagicMock records every attribute access.
    session = MagicMock()

    await log_event(
        session,
        actor_type="system",
        actor_id="http",
        action="http.request",
        target_type="endpoint",
        target_id="POST /v1/analyze",
        org_id=UUID("66666666-6666-6666-6666-666666666666"),
    )

    session.add.assert_called_once()
    # No mutation/commit/delete path is exercised by log_event.
    session.commit.assert_not_called()
    session.delete.assert_not_called()
    session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Middleware fixtures — mock the DB write seam so dispatch is DB-free.
# ---------------------------------------------------------------------------
def _patch_audit_seam(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace session_scope + log_event used by _write_audit_event.

    Returns a dict the test inspects: ``calls`` collects the org_id passed to
    each log_event invocation (and proves the http.request row landed).
    """
    captured: dict = {"calls": []}

    @asynccontextmanager
    async def _fake_session_scope():
        yield _CapturingSession()

    async def _fake_log_event(session, *, org_id=None, **kwargs):
        captured["calls"].append({"org_id": org_id, **kwargs})

    monkeypatch.setattr(middleware, "session_scope", _fake_session_scope)
    monkeypatch.setattr(middleware, "log_event", _fake_log_event)
    return captured


# ---------------------------------------------------------------------------
# 4. SAFE DEFAULT — tenancy_enabled=False ⇒ AuditMiddleware passes org_id=None
#    even when a stray org sits in the contextvar.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_middleware_off_passes_org_id_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, False)
    captured = _patch_audit_seam(monkeypatch)

    # Even if something left an org in the contextvar, the OFF path must ignore
    # it and stamp NULL — byte-identical to today.
    token = current_org_var.set("99999999-9999-9999-9999-999999999999")
    try:

        async def call_next(_request):
            return SimpleNamespace(status_code=200)

        mw = middleware.AuditMiddleware(app=lambda *a, **k: None)
        await mw.dispatch(_fake_request(), call_next)
    finally:
        current_org_var.reset(token)

    assert len(captured["calls"]) == 1
    assert captured["calls"][0]["org_id"] is None
    assert captured["calls"][0]["action"] == "http.request"


# ---------------------------------------------------------------------------
# 5. ENABLED ⇒ AuditMiddleware reads current_org_var and stamps that org.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_middleware_on_stamps_contextvar_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, True)
    captured = _patch_audit_seam(monkeypatch)

    org = "77777777-7777-7777-7777-777777777777"
    token = current_org_var.set(org)
    try:

        async def call_next(_request):
            return SimpleNamespace(status_code=201)

        mw = middleware.AuditMiddleware(app=lambda *a, **k: None)
        await mw.dispatch(_fake_request(), call_next)
    finally:
        current_org_var.reset(token)

    assert len(captured["calls"]) == 1
    assert captured["calls"][0]["org_id"] == org


# ---------------------------------------------------------------------------
# 6. The reversibility switch ships False ⇒ the write is AWAITED synchronously
#    (the http.request row has landed by the time dispatch returns).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_default_write_is_awaited_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guard the shipped default — this constant must NOT be flipped.
    assert middleware._AUDIT_FIRE_AND_FORGET is False

    _set_tenancy_enabled(monkeypatch, False)
    captured = _patch_audit_seam(monkeypatch)

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    mw = middleware.AuditMiddleware(app=lambda *a, **k: None)
    await mw.dispatch(_fake_request(), call_next)

    # No event-loop yield needed: with FIRE_AND_FORGET False the row is written
    # inline before dispatch returns. If it had been scheduled via create_task,
    # captured["calls"] would still be empty here.
    assert len(captured["calls"]) == 1
    assert captured["calls"][0]["action"] == "http.request"


# ---------------------------------------------------------------------------
# 7. Audit failures never break the response (audit_write_failed semantics).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audit_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tenancy_enabled(monkeypatch, False)

    @asynccontextmanager
    async def _boom_session_scope():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(middleware, "session_scope", _boom_session_scope)

    sentinel = SimpleNamespace(status_code=200)

    async def call_next(_request):
        return sentinel

    mw = middleware.AuditMiddleware(app=lambda *a, **k: None)
    # The response must still be returned despite the audit write blowing up.
    result = await mw.dispatch(_fake_request(), call_next)
    assert result is sentinel


# ---------------------------------------------------------------------------
# 8. Append-only contract untouched: storage/audit.py introduces no UPDATE or
#    DELETE statement (static guard against accidental mutation paths).
# ---------------------------------------------------------------------------
def test_audit_module_has_no_update_or_delete() -> None:
    import inspect

    src = inspect.getsource(audit_mod).upper()
    # The append-only trigger blocks UPDATE/DELETE on audit_event; the write
    # path must never emit them. (The check helper only SELECTs from pg_trigger.)
    assert "UPDATE AUDIT_EVENT" not in src
    assert "DELETE FROM AUDIT_EVENT" not in src
