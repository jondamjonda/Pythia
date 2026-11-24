"""WP-023 (T23) — session-scope org GUC injection.

Proves the SAFE-DEFAULT LAW for the request hot path:

  * rls_enforced=False (the local default)  ⇒ ZERO SET LOCAL app.current_org
    statements are issued ⇒ byte-identical to pre-Phase-A behaviour.
  * rls_enforced=True + current_org_var set  ⇒ the org GUC is the FIRST
    statement on the session, BEFORE anything else (e.g. hybrid.py's
    SET LOCAL hnsw.ef_search).
  * rls_enforced=True but NO org set         ⇒ still no bogus GUC.

These tests mock the AsyncSession so they run WITHOUT a database. The single
real-DB assertion at the bottom is guarded scratch-or-skip.
"""

from __future__ import annotations

import contextlib
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from regsuper.storage import database
from regsuper.storage.database import (
    current_org_var,
    is_platform_admin_var,
    session_scope,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _set_rls_enforced(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Force get_settings().rls_enforced to ``value`` for the duration of a test.

    database.py reads the flag via get_settings() at call time, so we patch the
    Settings object's attribute on the cached instance.

    The enforce path now also asserts the LEDGER-M1 invariant
    (``rls_enforced and not tenancy_enabled`` is a fail-closed lockout), so when
    we enforce RLS we also enable tenancy — the only consistent, supported combo.
    These tests exercise the GUC-injection mechanics, not the misconfig guard
    (that has its own coverage), so we set the valid pairing here.
    """
    settings = database.get_settings()
    monkeypatch.setattr(settings, "rls_enforced", value, raising=False)
    if value:
        monkeypatch.setattr(settings, "tenancy_enabled", True, raising=False)
        # These tests mock the AsyncSession (no DB), so the once-per-process
        # LEDGER-C1 role probe (_assert_connection_not_rls_exempt) cannot run
        # against a real pg_roles row. Mark the boot guard already-satisfied so
        # _apply_org_guc proceeds straight to the GUC-injection mechanics under
        # test. (The M1/C1 guards have their own dedicated coverage.)
        monkeypatch.setattr(database, "_rls_safety_checked", True, raising=False)


def _install_mock_factory(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace get_session_factory with one yielding a mock AsyncSession.

    Returns the mock session so the test can inspect ``session.execute`` calls.
    The mock supports the ``async with factory() as session`` protocol plus
    commit/rollback used by session_scope().
    """
    session = MagicMock(name="AsyncSession")
    session.execute = AsyncMock(name="execute")
    session.commit = AsyncMock(name="commit")
    session.rollback = AsyncMock(name="rollback")

    # `async with factory() as session:` — factory() returns an async CM.
    cm = MagicMock(name="session_cm")
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(name="session_factory", return_value=cm)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    return session


def _set_local_org_calls(session: MagicMock) -> list:
    """All execute() calls whose SQL text mentions app.current_org."""
    out = []
    for call in session.execute.await_args_list:
        stmt = call.args[0] if call.args else None
        sql = str(getattr(stmt, "text", stmt))
        if "app.current_org" in sql:
            out.append(call)
    return out


def _first_sql(session: MagicMock) -> str:
    call = session.execute.await_args_list[0]
    stmt = call.args[0]
    return str(getattr(stmt, "text", stmt))


# ---------------------------------------------------------------------------
# 1. SAFE DEFAULT — rls_enforced=False ⇒ NO GUC at all.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_op_when_rls_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_rls_enforced(monkeypatch, False)
    session = _install_mock_factory(monkeypatch)

    # Even with an org set, a disabled flag must issue nothing.
    tok = current_org_var.set("11111111-1111-1111-1111-111111111111")
    try:
        async with session_scope() as s:
            assert s is session
    finally:
        current_org_var.reset(tok)

    # ZERO app.current_org statements ⇒ byte-identical to today.
    assert _set_local_org_calls(session) == []
    # session_scope still commits on clean exit (unchanged behaviour).
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. ENFORCED + org set ⇒ GUC is the FIRST statement, with the right value.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_org_guc_is_first_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_rls_enforced(monkeypatch, True)
    session = _install_mock_factory(monkeypatch)

    org = "22222222-2222-2222-2222-222222222222"
    tok_o = current_org_var.set(org)
    tok_a = is_platform_admin_var.set(False)
    try:
        async with session_scope() as s:
            # Simulate a caller statement AFTER the GUC (e.g. hybrid.py ef_search).
            await s.execute("SET LOCAL hnsw.ef_search = 20")
    finally:
        current_org_var.reset(tok_o)
        is_platform_admin_var.reset(tok_a)

    # FIRST execute must be the org GUC, BEFORE the ef_search SET LOCAL.
    # Uses set_config(name, value, true) — SET LOCAL can't take a bound param.
    first = _first_sql(session)
    assert "set_config('app.current_org'" in first, first
    assert "ef_search" not in first

    # The bound value is the org we set.
    first_call = session.execute.await_args_list[0]
    assert first_call.args[1] == {"org": org}

    # is_platform_admin follows, defaulting to 'off' (passed as a bound param).
    second_call = session.execute.await_args_list[1]
    second = str(getattr(second_call.args[0], "text", ""))
    assert "set_config('app.is_platform_admin'" in second
    assert second_call.args[1] == {"adm": "off"}

    # Ordering: org GUC precedes the caller's ef_search statement.
    all_sql = [str(getattr(c.args[0], "text", c.args[0])) for c in session.execute.await_args_list]
    org_idx = next(i for i, s in enumerate(all_sql) if "app.current_org" in s)
    ef_idx = next(i for i, s in enumerate(all_sql) if "ef_search" in s)
    assert org_idx < ef_idx


# ---------------------------------------------------------------------------
# 3. ENFORCED + platform admin ⇒ is_platform_admin = 'on'.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_platform_admin_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_rls_enforced(monkeypatch, True)
    session = _install_mock_factory(monkeypatch)

    tok_o = current_org_var.set("33333333-3333-3333-3333-333333333333")
    tok_a = is_platform_admin_var.set(True)
    try:
        async with session_scope():
            pass
    finally:
        current_org_var.reset(tok_o)
        is_platform_admin_var.reset(tok_a)

    admin_calls = [
        c
        for c in session.execute.await_args_list
        if "app.is_platform_admin" in str(getattr(c.args[0], "text", c.args[0]))
    ]
    assert admin_calls, "expected an is_platform_admin set_config call"
    # value is a bound param now (set_config can take params; SET LOCAL cannot).
    assert admin_calls[0].args[1] == {"adm": "on"}


# ---------------------------------------------------------------------------
# 4. ENFORCED but NO org (contextvar default None) ⇒ no bogus GUC.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_enforced_but_no_org_issues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rls_enforced(monkeypatch, True)
    session = _install_mock_factory(monkeypatch)

    # current_org_var default is None — do NOT set it.
    assert current_org_var.get() is None
    async with session_scope():
        pass

    assert _set_local_org_calls(session) == []


# ---------------------------------------------------------------------------
# 5. get_db() path mirrors session_scope() (FastAPI dependency).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_db_applies_guc_when_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rls_enforced(monkeypatch, True)
    session = _install_mock_factory(monkeypatch)

    org = "44444444-4444-4444-4444-444444444444"
    tok = current_org_var.set(org)
    try:
        agen = database.get_db()
        s = await agen.__anext__()
        assert s is session
        with contextlib.suppress(StopAsyncIteration):
            await agen.__anext__()
    finally:
        current_org_var.reset(tok)

    assert "set_config('app.current_org'" in _first_sql(session)


@pytest.mark.asyncio
async def test_get_db_no_op_when_not_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rls_enforced(monkeypatch, False)
    session = _install_mock_factory(monkeypatch)

    tok = current_org_var.set("55555555-5555-5555-5555-555555555555")
    try:
        agen = database.get_db()
        await agen.__anext__()
        with contextlib.suppress(StopAsyncIteration):
            await agen.__anext__()
    finally:
        current_org_var.reset(tok)

    assert _set_local_org_calls(session) == []


# ---------------------------------------------------------------------------
# 6. Real-DB smoke (scratch-or-skip) — proves the GUC is actually visible via
#    current_setting() inside the same transaction. Skipped without a DB.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_guc_visible_in_real_db(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("REGSUPER_SCRATCH_DB"):
        pytest.skip("no scratch DB (set REGSUPER_SCRATCH_DB to run)")

    from sqlalchemy import text

    _set_rls_enforced(monkeypatch, True)
    org = "66666666-6666-6666-6666-666666666666"
    tok = current_org_var.set(org)
    try:
        async with session_scope() as s:
            got = (
                await s.execute(text("SELECT current_setting('app.current_org', true)"))
            ).scalar()
            assert got == org
    finally:
        current_org_var.reset(tok)
