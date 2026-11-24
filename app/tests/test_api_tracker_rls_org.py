"""RLS WITH-CHECK invariant on the api_call cost row (LEDGER-H6).

Under enforced RLS the ``p_api_call_tenant`` policy rejects an INSERT whose
``org_id`` does not match ``current_setting('app.current_org')``. ``_persist_row``
opens its own session (which stamps that GUC from ``current_org_var``), so a cost
row written with ``org_id=None`` while the request is org-scoped is rejected —
exactly the answer-path failure this guards against. ``_rls_org_fallback`` makes
the row's tenant match the active RLS org by construction.

These are pure (no DB): they exercise the org-resolution logic directly, which is
what decides whether the eventual INSERT passes the WITH CHECK.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from regsuper.observability.api_tracker import _rls_org_fallback
from regsuper.storage.database import current_org_var


def test_explicit_org_always_wins():
    """A caller-threaded org_id is never overridden (metered l2/l3 paths)."""
    explicit = uuid4()
    token = current_org_var.set(str(uuid4()))  # a DIFFERENT active org
    try:
        assert _rls_org_fallback(explicit) == explicit
    finally:
        current_org_var.reset(token)


def test_none_falls_back_to_active_rls_org():
    """org_id=None + an active GUC org ⇒ the row is stamped with that org, so the
    WITH CHECK (org_id = app.current_org) passes."""
    active = uuid4()
    token = current_org_var.set(str(active))
    try:
        assert _rls_org_fallback(None) == active
    finally:
        current_org_var.reset(token)


def test_safe_default_no_context_stays_null():
    """SAFE DEFAULT: no active org (default flags / TenancyMiddleware pass-through)
    ⇒ None, byte-identical to pre-RLS behaviour (row.org_id stays NULL)."""
    # current_org_var default is None; assert we do not invent an org.
    assert current_org_var.get() is None
    assert _rls_org_fallback(None) is None


def test_malformed_context_value_never_raises():
    """The tracker must never break the caller — a non-UUID contextvar value
    falls back to None rather than raising."""
    token = current_org_var.set("not-a-uuid")
    try:
        assert _rls_org_fallback(None) is None
    finally:
        current_org_var.reset(token)


def test_returns_uuid_type():
    active = uuid4()
    token = current_org_var.set(str(active))
    try:
        out = _rls_org_fallback(None)
        assert isinstance(out, UUID)
    finally:
        current_org_var.reset(token)
