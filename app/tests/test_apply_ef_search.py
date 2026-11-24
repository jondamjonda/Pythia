"""C1-T02 — the ef_search precedence guard (storage.database.apply_ef_search).

Proves the org-GUC-FIRST ordering for the retrieval hot path:

  * rls_enforced=False (default) ⇒ apply_ef_search issues EXACTLY one statement,
    the `SET LOCAL hnsw.ef_search`, and ZERO org GUC — byte-identical to the raw
    `SET LOCAL hnsw.ef_search` hybrid.py issues today.
  * rls_enforced=True + org set   ⇒ the org GUC is issued BEFORE the ef_search SET
    (the precedence guarantee), and the ef_search value is the clamped int.

DB-free: the AsyncSession is mocked, like test_session_scope_guc.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from regsuper.storage import database
from regsuper.storage.database import (
    apply_ef_search,
    current_org_var,
    is_platform_admin_var,
)


def _set_rls(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    settings = database.get_settings()
    monkeypatch.setattr(settings, "rls_enforced", value, raising=False)
    if value:
        monkeypatch.setattr(settings, "tenancy_enabled", True, raising=False)
        # No real DB here; mark the once-per-process C1 role probe satisfied so
        # _apply_org_guc proceeds to the GUC-injection mechanics under test.
        monkeypatch.setattr(database, "_rls_safety_checked", True, raising=False)


def _mock_session() -> MagicMock:
    session = MagicMock(name="AsyncSession")
    session.execute = AsyncMock(name="execute")
    return session


def _sql_list(session: MagicMock) -> list[str]:
    return [str(getattr(c.args[0], "text", c.args[0])) for c in session.execute.await_args_list]


# ---------------------------------------------------------------------------
# Default (not enforced) ⇒ ONLY the ef_search SET, no org GUC.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_op_org_guc_when_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_rls(monkeypatch, False)
    session = _mock_session()

    # Even with an org set, a disabled flag must issue no GUC.
    tok = current_org_var.set("11111111-1111-1111-1111-111111111111")
    try:
        await apply_ef_search(session, 20)
    finally:
        current_org_var.reset(tok)

    sql = _sql_list(session)
    assert len(sql) == 1, sql
    assert "hnsw.ef_search = 20" in sql[0]
    assert not any("app.current_org" in s for s in sql)


# ---------------------------------------------------------------------------
# Enforced ⇒ org GUC precedes the ef_search SET.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_org_guc_precedes_ef_search_when_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_rls(monkeypatch, True)
    session = _mock_session()

    org = "22222222-2222-2222-2222-222222222222"
    tok_o = current_org_var.set(org)
    tok_a = is_platform_admin_var.set(False)
    try:
        await apply_ef_search(session, 50)
    finally:
        current_org_var.reset(tok_o)
        is_platform_admin_var.reset(tok_a)

    sql = _sql_list(session)
    org_idx = next(i for i, s in enumerate(sql) if "app.current_org" in s)
    ef_idx = next(i for i, s in enumerate(sql) if "ef_search" in s)
    assert org_idx < ef_idx, sql
    assert "hnsw.ef_search = 50" in sql[ef_idx]


# ---------------------------------------------------------------------------
# ef_search is clamped to >= 1 (matches hybrid.py's `max(1, int(...))`).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ef_search_clamped_to_minimum_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_rls(monkeypatch, False)
    session = _mock_session()
    await apply_ef_search(session, 0)
    sql = _sql_list(session)
    assert "hnsw.ef_search = 1" in sql[0]
