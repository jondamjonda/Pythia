"""AQ-05 — GET /api/municipalities?q= search endpoint (DB-free).

Covers:
  1. Typing "Plz" returns Plzeň with its code (fake gazetteer, case/diacritic-
     insensitive fold applied to the query prefix).
  2. An unknown town → empty list (200, not 500).
  3. Blank/empty q → empty list (no query issued).
  4. The route is AUTHED + org-scoped: it calls get_principal, and under
     require_auth=True an unauthenticated request is denied (401).
  5. The profile-save 422-on-unknown-municipality_code pin is STILL intact
     (asserted here too so this file guards it alongside test_municipality_gazetteer).

All DB-free: a fake session filters an in-memory gazetteer with the SAME fold as
the SQL, so no Postgres is required.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import auth as auth_api
from regsuper.api import municipalities as muni_api
from regsuper.api import profiles as profiles_api

# A tiny in-memory gazetteer: (code, name, okres, orp, kraj).
_GAZ = [
    ("554791", "Plzeň", "Plzeň-město", "Plzeň", "Plzeňský kraj"),
    ("582786", "Brno", "Brno-město", "Brno", "Jihomoravský kraj"),
    ("500011", "Plasy", "Plzeň-sever", "Kaznějov", "Plzeňský kraj"),
]


def _fold(value: str) -> str:
    """Mirror muni_api._fold so the fake session matches like the real SQL."""
    return muni_api._fold(value)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Async session double: `execute(stmt, params)` runs the fold-prefix filter
    over the in-memory gazetteer, exact-match-first ordering, cap at _MAX_ROWS."""

    async def execute(self, stmt, params):
        prefix = params["prefix"].rstrip("%")
        exact = params["exact"]
        hits = [row for row in _GAZ if _fold(row[1]).startswith(prefix)]
        hits.sort(key=lambda row: (0 if _fold(row[1]) == exact else 1, row[1]))
        return _FakeResult(hits[: muni_api._MAX_ROWS])


class _FakeScope:
    """Async-context-manager wrapper yielding a fixed session (mirrors session_scope())."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _muni_client() -> TestClient:
    app = FastAPI()
    app.include_router(muni_api.router)
    return TestClient(app, raise_server_exceptions=True)


def _settings_local():
    """require_auth=False (default local mode) → get_principal is a pass-through."""
    return SimpleNamespace(require_auth=False)


def _settings_enforced():
    return SimpleNamespace(require_auth=True)


# ── 1. Typing "Plz" returns Plzeň with its code ──────────────────────────────
def test_search_prefix_returns_plzen_with_code():
    session = _FakeSession()
    with (
        patch.object(auth_api, "get_settings", _settings_local),
        patch.object(muni_api, "session_scope", lambda: _FakeScope(session)),
    ):
        resp = _muni_client().get("/api/municipalities", params={"q": "Plz"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {row["name"]: row for row in body}
    assert "Plzeň" in names
    assert names["Plzeň"]["code"] == "554791"
    assert names["Plzeň"]["kraj"] == "Plzeňský kraj"
    # Exact whole-word match sorts first even though "Plasy" also prefix-matches.
    assert body[0]["name"] == "Plzeň"


def test_search_is_diacritic_insensitive():
    """A diacritic-typed prefix ("Plž") still folds to match "Plzeň"/"Plasy"."""
    session = _FakeSession()
    with (
        patch.object(auth_api, "get_settings", _settings_local),
        patch.object(muni_api, "session_scope", lambda: _FakeScope(session)),
    ):
        resp = _muni_client().get("/api/municipalities", params={"q": "PLŽ"})
    assert resp.status_code == 200, resp.text
    assert any(row["name"] == "Plzeň" for row in resp.json())


# ── 2. Unknown town → empty list (200, not 500) ──────────────────────────────
def test_search_unknown_town_returns_empty_list():
    session = _FakeSession()
    with (
        patch.object(auth_api, "get_settings", _settings_local),
        patch.object(muni_api, "session_scope", lambda: _FakeScope(session)),
    ):
        resp = _muni_client().get("/api/municipalities", params={"q": "Zzzznope"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ── 3. Blank q → empty list, no query issued ─────────────────────────────────
def test_search_blank_query_returns_empty_without_db():
    class _BoomSession:
        async def execute(self, *a, **k):  # must NOT be reached
            raise AssertionError("blank q should short-circuit before any DB read")

    with (
        patch.object(auth_api, "get_settings", _settings_local),
        patch.object(muni_api, "session_scope", lambda: _FakeScope(_BoomSession())),
    ):
        resp = _muni_client().get("/api/municipalities", params={"q": "   "})
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ── 4. The route is authed + org-scoped (401 under require_auth=True) ─────────
def test_search_denies_unauthenticated_when_auth_enforced():
    """get_principal runs first; with require_auth=True and no session cookie the
    sibling-auth path raises 401 — proving the endpoint is authed/org-scoped."""
    session = _FakeSession()
    with (
        patch.object(auth_api, "get_settings", _settings_enforced),
        patch.object(muni_api, "session_scope", lambda: _FakeScope(session)),
    ):
        resp = _muni_client().get("/api/municipalities", params={"q": "Plz"})
    assert resp.status_code == 401, resp.text


# ── 5. The profile-save 422-on-unknown-municipality_code pin is STILL intact ──
class _FakeProfileSession:
    """get(Municipality, code) returns a stub only for known codes; add/flush no-op."""

    def __init__(self, known: set[str]):
        self._known = known
        self._added: list = []

    async def get(self, model, code):
        return SimpleNamespace(code=code) if code in self._known else None

    def add(self, obj):
        self._added.append(obj)

    async def flush(self):
        for obj in self._added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()


async def _anoop(*a, **k):
    return None


def _profiles_client() -> TestClient:
    app = FastAPI()
    app.include_router(profiles_api.router)
    return TestClient(app, raise_server_exceptions=True)


def test_profile_save_still_422s_on_unknown_municipality_code():
    """PIN: the autocomplete never weakens the profile-save validation — an
    unknown code on create is still a 422 (mirrors test_municipality_gazetteer)."""
    session = _FakeProfileSession(known={"554791"})  # 999999 is NOT known
    with (
        patch.object(profiles_api, "get_settings", lambda: SimpleNamespace(profiles_enabled=True)),
        patch.object(profiles_api, "session_scope", lambda: _FakeScope(session)),
        patch.object(profiles_api, "emit", _anoop),
    ):
        resp = _profiles_client().post(
            "/api/profiles",
            json={"name": "Acme", "municipality_code": "999999"},
        )
    assert resp.status_code == 422, resp.text
    assert "municipality_code" in resp.text


def test_profile_save_accepts_known_municipality_code():
    session = _FakeProfileSession(known={"554791"})
    with (
        patch.object(profiles_api, "get_settings", lambda: SimpleNamespace(profiles_enabled=True)),
        patch.object(profiles_api, "session_scope", lambda: _FakeScope(session)),
        patch.object(profiles_api, "emit", _anoop),
    ):
        resp = _profiles_client().post(
            "/api/profiles",
            json={"name": "Acme", "municipality_code": "554791"},
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["municipality_code"] == "554791"
