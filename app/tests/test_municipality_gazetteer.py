"""AQ-05 — municipality gazetteer loader + profile-API code validation + the
AnswerContext municipality_code mapping. All DB-free (mocked rows / fake session).

Covers:
  1. Loader `parse_rows` normalisation + idempotence (dedup on `code`, drop rows
     missing code/name, trim/empty→None).
  2. Profile API accepts a KNOWN municipality code and rejects an UNKNOWN one
     (422), using a fake session so no DB is touched.
  3. AnswerContext.from_business_profile maps `municipality_code`, preferring the
     validated code and falling back to the legacy free-text field.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.analysis.answer_context import AnswerContext
from regsuper.api import profiles as profiles_api
from regsuper.ingest import municipality_gazetteer as gaz


# ── 1. Loader `parse_rows` (pure, DB-free) ───────────────────────────────────
def _seed_row(code="554791", name="Plzeň", **over):
    base = dict(
        code=code,
        name=name,
        okres_code="CZ0323",
        okres_name="Plzeň-město",
        orp_code="3209",
        orp_name="Plzeň",
        kraj_code="CZ032",
        kraj_name="Plzeňský kraj",
        okres_csu_code="40371",
        kraj_csu_code="3042",
    )
    base.update(over)
    return base


def test_parse_rows_normalises_and_keeps_all_columns():
    rows = gaz.parse_rows([_seed_row()])
    assert len(rows) == 1
    r = rows[0]
    assert set(r.keys()) == set(gaz._COLUMNS)
    assert r["code"] == "554791"
    assert r["name"] == "Plzeň"
    assert r["okres_code"] == "CZ0323"
    assert r["kraj_name"] == "Plzeňský kraj"


def test_parse_rows_trims_and_blanks_to_none():
    r = gaz.parse_rows([_seed_row(orp_name="  ", okres_name=" Plzeň-město ")])[0]
    assert r["orp_name"] is None  # whitespace → None
    assert r["okres_name"] == "Plzeň-město"  # trimmed


def test_parse_rows_skips_rows_missing_code_or_name():
    rows = gaz.parse_rows(
        [
            _seed_row(code="", name="NoCode"),
            _seed_row(code="500011", name=""),
            _seed_row(code="582786", name="Brno"),
        ]
    )
    assert [r["code"] for r in rows] == ["582786"]


def test_parse_rows_dedups_on_code_last_wins():
    """Idempotence at the parse layer: two rows with the same code collapse to one
    (last wins), so the PK UPSERT never sees a duplicate."""
    rows = gaz.parse_rows(
        [
            _seed_row(code="554791", kraj_name="OLD"),
            _seed_row(code="554791", kraj_name="Plzeňský kraj"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["kraj_name"] == "Plzeňský kraj"


# ── 2. Profile API accepts a known code / rejects an unknown one ─────────────
class _FakeSession:
    """A minimal async session double: `get(Municipality, code)` returns a stub
    only for codes in `known`; everything else (add/flush/execute) is a no-op."""

    def __init__(self, known: set[str]):
        self._known = known
        self._added: list = []

    async def get(self, model, code):
        return SimpleNamespace(code=code) if code in self._known else None

    def add(self, obj):
        self._added.append(obj)

    async def flush(self):
        # Mimic SQLAlchemy: populate the PK default on flush so the response model
        # (which needs a UUID id) validates.
        for obj in self._added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()


class _FakeScope:
    """Async-context-manager wrapper yielding a fixed session (mirrors session_scope())."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _settings_on():
    return SimpleNamespace(profiles_enabled=True)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(profiles_api.router)
    return TestClient(app, raise_server_exceptions=True)


def test_create_profile_accepts_known_municipality_code():
    session = _FakeSession(known={"554791"})
    with (
        patch.object(profiles_api, "get_settings", _settings_on),
        patch.object(profiles_api, "session_scope", lambda: _FakeScope(session)),
        patch.object(profiles_api, "emit", _anoop),
    ):
        resp = _client().post(
            "/api/profiles",
            json={"name": "Acme", "municipality_code": "554791"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["municipality_code"] == "554791"


def test_create_profile_rejects_unknown_municipality_code():
    session = _FakeSession(known={"554791"})  # 999999 is NOT known
    with (
        patch.object(profiles_api, "get_settings", _settings_on),
        patch.object(profiles_api, "session_scope", lambda: _FakeScope(session)),
        patch.object(profiles_api, "emit", _anoop),
    ):
        resp = _client().post(
            "/api/profiles",
            json={"name": "Acme", "municipality_code": "999999"},
        )
    assert resp.status_code == 422, resp.text
    assert "municipality_code" in resp.text


def test_create_profile_allows_null_municipality_code():
    session = _FakeSession(known=set())
    with (
        patch.object(profiles_api, "get_settings", _settings_on),
        patch.object(profiles_api, "session_scope", lambda: _FakeScope(session)),
        patch.object(profiles_api, "emit", _anoop),
    ):
        resp = _client().post("/api/profiles", json={"name": "Acme"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["municipality_code"] is None


async def _anoop(*a, **k):  # emit() double
    return None


# ── 3. AnswerContext maps municipality_code ──────────────────────────────────
def test_answer_context_maps_validated_code():
    profile = SimpleNamespace(municipality_code="554791", registered_municipality="Plzeň")
    ctx = AnswerContext.from_business_profile(profile)
    assert ctx.municipality_code == "554791"  # validated code preferred


def test_answer_context_falls_back_to_legacy_free_text():
    profile = SimpleNamespace(municipality_code=None, registered_municipality="Plzeň")
    ctx = AnswerContext.from_business_profile(profile)
    assert ctx.municipality_code == "Plzeň"  # legacy fallback


def test_answer_context_none_when_neither_present():
    profile = SimpleNamespace()
    ctx = AnswerContext.from_business_profile(profile)
    assert ctx.municipality_code is None
