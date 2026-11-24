"""ENT-3 — scopes_for_org: ∅ for non-Conduit (api_access False), granted set
otherwise (WP-044/T-C2a). Pure projection; DB-free.
"""

from __future__ import annotations

import pytest

from regsuper.entitlements.models import EffectiveEntitlement
from regsuper.entitlements.resolver import scopes_for_org

_DEFAULT_SCOPES = {"legislation.read", "obligations.read", "answer"}


def _ent(api_access: bool, metadata: dict | None = None) -> EffectiveEntitlement:
    return EffectiveEntitlement(api_access=api_access, metadata=metadata or {})


def test_lite_has_empty_scope_set():
    assert scopes_for_org(_ent(api_access=False)) == set()


def test_business_has_empty_scope_set():
    # Business does NOT have api_access in the seed → ∅.
    assert scopes_for_org(_ent(api_access=False)) == set()


def test_advisor_grants_the_default_scope_set():
    assert scopes_for_org(_ent(api_access=True)) == _DEFAULT_SCOPES


def test_downgraded_api_access_yields_empty_set():
    """An org whose api_access flips False mints no scopes (the security point:
    a downgraded org cannot mint a Conduit token)."""
    assert scopes_for_org(_ent(api_access=True)) != set()
    assert scopes_for_org(_ent(api_access=False)) == set()


def test_per_org_scope_narrowing_via_metadata():
    """When entitlement.metadata['scopes'] is set, that narrowed set is granted
    (per-org grant), still only when api_access is True."""
    ent = _ent(api_access=True, metadata={"scopes": ["legislation.read"]})
    assert scopes_for_org(ent) == {"legislation.read"}


def test_metadata_scopes_ignored_when_no_api_access():
    """metadata scopes never grant when api_access is False (∅ wins)."""
    ent = _ent(api_access=False, metadata={"scopes": ["answer"]})
    assert scopes_for_org(ent) == set()


def test_scopes_rejects_bare_org_id():
    from uuid import uuid4

    with pytest.raises(TypeError):
        scopes_for_org(uuid4())
