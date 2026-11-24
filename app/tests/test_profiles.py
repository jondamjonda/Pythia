"""WP-049 — SME business-profile CRUD + profile-driven (synchronous) analyze.

Three layers, mirroring `test_audit_export.py`:

1. **Gated-off surface** (DB-free, router-only mount): with `get_settings` patched
   `profiles_enabled=False`, every one of the FIVE routes is 404 (inert by default).
   A valid JSON body is sent for POST/PATCH so the 404 comes from the gate, not from
   request validation.

2. **Pure helpers** (DB-free): `build_project_from_profile` maps a profile onto a
   Project deterministically (jurisdiction scope, entity type, sector/tag derivation,
   the `business_profile` provenance metadata), and `_jurisdiction_scope` maps the
   four primary-jurisdiction values.

3. **Skip-if-no-DB roundtrip** (rollback-only): inside a transaction that is ALWAYS
   rolled back, with `emit` patched to a no-op, a BusinessProfile is added/flushed and
   its derive-tag columns + a soft-delete metadata write round-trip — proving the SQL
   shapes are valid WITHOUT mutating live data.

No live analyze test is written (it would invoke the real LLM cascade): analyze
coverage is the gated-off 404 + the `build_project_from_profile` pure test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import profiles as profiles_api


# ── settings double ──────────────────────────────────────────────────────────
def _settings(**over):
    base = dict(profiles_enabled=False)
    base.update(over)
    return SimpleNamespace(**base)


# ── helpers ──────────────────────────────────────────────────────────────────
def _profile_double(**over):
    """A duck-typed BusinessProfile carrying every attr the helpers read."""
    base = dict(
        id=uuid4(),
        org_id=uuid4(),
        display_name="Acme",
        sector_override=None,
        primary_jurisdiction="CZ+EU",
        legal_form="sro",
        analysis_depth="standard",
        nace_code="C25",
        processes_personal_data=True,
        handles_client_money=False,
        has_real_estate=False,
        cross_border=True,
        is_employer=True,
        additional_activities=[],
        regulated_activity=None,
        derived_sector_tags=[],
        derived_activity_tags=[],
    )
    base.update(over)
    return SimpleNamespace(**base)


def _client() -> TestClient:
    """A TestClient over JUST this router — no FastAPI lifespan, so no DB at import."""
    app = FastAPI()
    app.include_router(profiles_api.router)
    return TestClient(app, raise_server_exceptions=True)


# ── 1. Gated-off surface (DB-free, router-only mount) ────────────────────────
def test_post_profile_404_when_flag_off() -> None:
    with patch.object(profiles_api, "get_settings", lambda: _settings(profiles_enabled=False)):
        resp = _client().post("/api/profiles", json={"name": "Acme"})
    assert resp.status_code == 404, resp.text


def test_get_profile_404_when_flag_off() -> None:
    with patch.object(profiles_api, "get_settings", lambda: _settings(profiles_enabled=False)):
        resp = _client().get(f"/api/profiles/{uuid4()}")
    assert resp.status_code == 404, resp.text


def test_patch_profile_404_when_flag_off() -> None:
    with patch.object(profiles_api, "get_settings", lambda: _settings(profiles_enabled=False)):
        resp = _client().patch(f"/api/profiles/{uuid4()}", json={"name": "New"})
    assert resp.status_code == 404, resp.text


def test_delete_profile_404_when_flag_off() -> None:
    with patch.object(profiles_api, "get_settings", lambda: _settings(profiles_enabled=False)):
        resp = _client().delete(f"/api/profiles/{uuid4()}")
    assert resp.status_code == 404, resp.text


def test_post_analyze_404_when_flag_off() -> None:
    with patch.object(profiles_api, "get_settings", lambda: _settings(profiles_enabled=False)):
        resp = _client().post(f"/api/profiles/{uuid4()}/analyze", json={})
    assert resp.status_code == 404, resp.text


# ── 2. Pure helpers (DB-free) ────────────────────────────────────────────────
def test_jurisdiction_scope_mapping() -> None:
    assert profiles_api._jurisdiction_scope("CZ") == ["CZ"]
    assert profiles_api._jurisdiction_scope("EU") == ["EU"]
    assert profiles_api._jurisdiction_scope("CZ+EU") == ["CZ", "EU"]
    assert profiles_api._jurisdiction_scope(None) == ["CZ"]


def test_build_project_from_profile_maps_fields() -> None:
    profile = _profile_double()
    fallback = uuid4()
    project = profiles_api.build_project_from_profile(profile, fallback_org_id=fallback)

    assert project.business_profile_id == profile.id
    assert project.name == "Acme"
    assert project.org_id == profile.org_id  # profile org wins over fallback
    assert project.jurisdiction_scope == ["CZ", "EU"]
    assert project.entity_type == "sro"
    assert project.analysis_depth == "standard"
    assert project.metadata_["source"] == "business_profile"
    assert project.metadata_["business_profile_id"] == str(profile.id)
    assert project.metadata_["intake_version"] == 1
    # NACE "C25" → manufacturing sector, so the sector + sector_tags are non-empty.
    assert project.sector
    assert project.sector != "general"
    assert project.metadata_["sector_tags"]


def test_build_project_uses_fallback_org_when_profile_has_none() -> None:
    profile = _profile_double(org_id=None)
    fallback = uuid4()
    project = profiles_api.build_project_from_profile(profile, fallback_org_id=fallback)
    assert project.org_id == fallback


def test_build_project_sector_override_wins() -> None:
    profile = _profile_double(sector_override="fintech")
    project = profiles_api.build_project_from_profile(profile, fallback_org_id=uuid4())
    assert project.sector == "fintech"


# ── 2b. Response / soft-delete pure helpers ──────────────────────────────────
def test_to_response_coerces_out_of_vocab_values() -> None:
    """A legacy row (`osvč` legal_form, `2-9` headcount) must NOT 500 the response —
    the out-of-vocab values are coerced to None for the response Literal."""
    profile = _profile_double(
        legal_form="osvč",
        headcount_band="2-9",
        ico="12345678",
        preferred_language="cs",
        derived_sector_tags=["manufacturing"],
        derived_activity_tags=["commercial"],
        metadata_={},
    )
    resp = profiles_api._to_response(profile)
    assert resp.name == "Acme"
    assert resp.legal_form is None  # 'osvč' not in the response vocab
    assert resp.headcount_band is None  # '2-9' not in the response vocab
    assert resp.sector_tags == ["manufacturing"]
    assert resp.activity_tags == ["commercial"]


def test_to_response_keeps_in_vocab_values() -> None:
    profile = _profile_double(
        legal_form="sro",
        headcount_band="10-49",
        ico=None,
        preferred_language="en",
        derived_sector_tags=[],
        derived_activity_tags=[],
        metadata_={},
    )
    resp = profiles_api._to_response(profile)
    assert resp.legal_form == "sro"
    assert resp.headcount_band == "10-49"
    assert resp.preferred_language == "en"


def test_is_deleted_detects_soft_delete_marker() -> None:
    assert profiles_api._is_deleted(_profile_double(metadata_={})) is False
    assert profiles_api._is_deleted(_profile_double(metadata_=None)) is False
    assert (
        profiles_api._is_deleted(
            _profile_double(metadata_={"deleted_at": "2026-06-25T00:00:00+00:00"})
        )
        is True
    )


# ── 3. One real DB roundtrip (skip-if-no-DB, rollback-only) ──────────────────
async def _db_reachable() -> bool:
    from sqlalchemy import text

    from regsuper.storage.database import dispose_engine, get_session_factory

    await dispose_engine()
    try:
        factory = get_session_factory()
        async with factory() as s:
            ok = (
                await s.execute(text("SELECT to_regclass('business_profile') IS NOT NULL"))
            ).scalar()
        return bool(ok)
    except Exception:
        return False


@pytest.mark.asyncio
async def test_business_profile_roundtrip_rolled_back() -> None:
    """A BusinessProfile add/flush proves the derive-tag columns + a soft-delete
    metadata write round-trip against a real session, inside a transaction that is
    ALWAYS rolled back so the live table is never mutated."""
    if not await _db_reachable():
        pytest.skip("no DB / business_profile table not reachable")

    from regsuper.storage.database import get_session_factory
    from regsuper.storage.models import BusinessProfile, Organization

    factory = get_session_factory()
    async with factory() as session:
        trans = await session.begin()
        try:
            # A valid org_id satisfies the FK; reuse an existing one.
            org_id = (
                await session.execute(__import__("sqlalchemy").select(Organization.id).limit(1))
            ).scalar()
            if org_id is None:
                pytest.skip("no organization row to attribute the profile to")

            profile = BusinessProfile(
                org_id=org_id,
                display_name="Roundtrip s.r.o.",
                legal_form="sro",
                nace_code="C25",
                headcount_band="1-9",
                primary_jurisdiction="CZ+EU",
                preferred_language="cs",
                processes_personal_data=True,
                is_employer=True,
            )
            profiles_api._apply_tags(profile)
            session.add(profile)
            await session.flush()

            assert profile.id is not None
            # The derive-tag columns persisted (NACE "C" → manufacturing sector tags).
            assert profile.derived_sector_tags
            assert profile.derived_activity_tags

            # A soft-delete metadata write round-trips (reassign so JSONB change is seen).
            profile.metadata_ = {
                **(profile.metadata_ or {}),
                "deleted_at": "2026-06-25T00:00:00+00:00",
            }
            await session.flush()
            refetched = await session.get(BusinessProfile, profile.id)
            assert profiles_api._is_deleted(refetched) is True
        finally:
            await trans.rollback()


# ── 4. VL-A-11 / A3.5 write-back "only-fill-EMPTY-field" guard (DB-free) ──────
def test_field_is_empty_semantics() -> None:
    """Empty = None or a whitespace-only string; any populated value is NOT empty."""
    assert profiles_api._field_is_empty(None) is True
    assert profiles_api._field_is_empty("") is True
    assert profiles_api._field_is_empty("   ") is True
    assert profiles_api._field_is_empty("C25") is False
    assert profiles_api._field_is_empty("sro") is False
    # A bool (a characteristic gate) is never scalar-"empty".
    assert profiles_api._field_is_empty(False) is False
    assert profiles_api._field_is_empty(True) is False


def test_suggestion_may_fill_only_when_field_empty() -> None:
    """A write-back suggestion may fill a scalar field ONLY when it is empty; a
    populated field (and any boolean gate) is off-limits."""
    empty = _profile_double(nace_code=None, municipality_code="  ", display_name="")
    assert profiles_api._suggestion_may_fill(empty, "nace_code") is True
    assert profiles_api._suggestion_may_fill(empty, "municipality_code") is True
    assert profiles_api._suggestion_may_fill(empty, "name") is True

    populated = _profile_double(nace_code="C25", municipality_code="554782")
    assert profiles_api._suggestion_may_fill(populated, "nace_code") is False
    assert profiles_api._suggestion_may_fill(populated, "municipality_code") is False

    # An unknown field / a boolean gate is never fillable by a suggestion.
    assert profiles_api._suggestion_may_fill(empty, "processes_personal_data") is False
    assert profiles_api._suggestion_may_fill(empty, "not_a_field") is False


# ── The guard is ENFORCED in the PATCH handler (DB-free, fake session) ────────
class _FakeSession:
    """A minimal async-context session stand-in: `get()` returns the seeded profile;
    everything else the handler needs is a no-op. Lets the real patch_profile route
    run WITHOUT a live DB so the server-side guard is exercised end-to-end."""

    def __init__(self, profile: Any) -> None:
        self._profile = profile

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self._profile

    async def execute(self, *_a: Any, **_k: Any) -> Any:  # for _validate_municipality_code
        class _R:
            def scalar(self_inner) -> Any:
                return "554782"  # any non-None → municipality validation passes

        return _R()


def _patch_client(profile: Any):
    """A TestClient over the profiles router with `profiles_enabled=True`, the DB
    session replaced by a fake backed by `profile`, auth + emit stubbed. Returns the
    (client, profile) so the caller can PATCH and then assert on the mutated double."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_scope():
        yield _FakeSession(profile)

    async def _fake_principal(_request: Any) -> Any:
        return SimpleNamespace(org_id=uuid4())

    async def _fake_emit(*_a: Any, **_k: Any) -> None:
        return None

    stack = [
        patch.object(profiles_api, "get_settings", lambda: _settings(profiles_enabled=True)),
        patch.object(profiles_api, "session_scope", _fake_scope),
        patch.object(profiles_api, "get_principal", _fake_principal),
        patch.object(profiles_api, "emit", _fake_emit),
    ]
    for p in stack:
        p.start()
    client = _client()
    return client, stack


def _patch_double(**over):
    """A profile double carrying the mutable columns the PATCH handler touches."""
    return _profile_double(
        headcount_band="1-9",
        preferred_language="cs",
        municipality_code=None,
        derived_sector_tags=[],
        derived_activity_tags=[],
        metadata_={},
        updated_at=None,
        **over,
    )


def test_patch_suggestion_fills_empty_field() -> None:
    """suggested_only=True + an EMPTY target (nace_code=None) → the field is filled."""
    profile = _patch_double(nace_code=None)
    client, stack = _patch_client(profile)
    try:
        resp = client.patch(
            f"/api/profiles/{uuid4()}",
            json={"nace_code": "C25", "suggested_only": True},
        )
        assert resp.status_code == 200, resp.text
        # The empty field was filled from the suggestion.
        assert profile.nace_code == "C25"
    finally:
        for p in stack:
            p.stop()


def test_patch_suggestion_never_overwrites_populated_field() -> None:
    """suggested_only=True + a POPULATED target (nace_code="C25") → NO-OP: the
    server-side guard NEVER overwrites the SME's own standing value."""
    profile = _patch_double(nace_code="C25")
    client, stack = _patch_client(profile)
    try:
        resp = client.patch(
            f"/api/profiles/{uuid4()}",
            json={"nace_code": "J62", "suggested_only": True},
        )
        assert resp.status_code == 200, resp.text
        # The populated value is UNTOUCHED — the suggestion was a no-op.
        assert profile.nace_code == "C25"
    finally:
        for p in stack:
            p.stop()


def test_patch_normal_edit_still_overwrites() -> None:
    """Without suggested_only the normal (self/operator) edit overwrites as before —
    the guard is scoped to the write-back path only."""
    profile = _patch_double(nace_code="C25")
    client, stack = _patch_client(profile)
    try:
        resp = client.patch(
            f"/api/profiles/{uuid4()}",
            json={"nace_code": "J62"},
        )
        assert resp.status_code == 200, resp.text
        assert profile.nace_code == "J62"
    finally:
        for p in stack:
            p.stop()
