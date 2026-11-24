"""WP-022 / A2-T22 — product-auth Principal (the RLS-GUC org_id source).

These tests pin the PHASE-A safe-default law for `get_principal` and the
auth primitives, DB-free where possible:

  * `require_auth=False` (default / only legal local mode) ⇒ `get_principal`
    returns the anonymous SENTINEL-org principal, NO 401, NO DB read.
  * `require_auth=True` + no/invalid session ⇒ fail CLOSED with 401 (the
    no-token branch needs no DB, so it runs offline).
  * scrypt password hash round-trips and verifies in constant time; a wrong
    password / NULL (SSO) / malformed hash all return False (never raise).
  * `projects.py` no longer imports or uses `uuid4` for the org — it derives
    the org from the `Principal` (static-source assertion, no app boot).

The one real-DB path (resolving a live session row) follows the
scratch-DB-or-skip guard from tests/test_migration_chain.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from uuid import UUID

import pytest

from regsuper.api import auth as auth_mod
from regsuper.api.auth import (
    Principal,
    _hash_password,
    _verify_password,
    anonymous_principal,
    get_principal,
    hash_token,
)

# The migration-007 backfill sentinel org — the anonymous principal MUST use it
# so the project.org_id FK resolves and local projects share the legacy tenant.
SENTINEL_ORG = UUID("00000000-0000-0000-0000-000000000001")


@dataclass
class _FakeSettings:
    require_auth: bool


class _FakeRequest:
    """Minimal stand-in for starlette.Request: just cookies + headers + client."""

    def __init__(self, cookies=None, headers=None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = None


# ── anonymous / pass-through (require_auth=False) ────────────────────────────
def test_anonymous_principal_uses_sentinel_org() -> None:
    p = anonymous_principal()
    assert isinstance(p, Principal)
    assert p.org_id == SENTINEL_ORG, (
        "anonymous principal must use the migration-007 sentinel org "
        "(00000000-…-0001) so project.org_id FK resolves"
    )
    assert p.user_id == UUID("00000000-0000-0000-0000-000000000000")
    assert p.is_platform_admin is False


def test_get_principal_passthrough_when_auth_disabled(monkeypatch) -> None:
    """require_auth=False ⇒ sentinel-org principal, NO 401, NO DB read."""
    monkeypatch.setattr(auth_mod, "get_settings", lambda: _FakeSettings(require_auth=False))
    # If this touched the DB it would blow up offline; it must not.
    monkeypatch.setattr(
        auth_mod,
        "session_scope",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DB read when require_auth=False")),
    )
    p = asyncio.run(get_principal(_FakeRequest()))
    assert p.org_id == SENTINEL_ORG
    # Even with a (bogus) token present, the disabled path never looks it up.
    p2 = asyncio.run(get_principal(_FakeRequest(cookies={auth_mod.SESSION_COOKIE: "bogus"})))
    assert p2.org_id == SENTINEL_ORG


# ── fail-closed (require_auth=True) ──────────────────────────────────────────
def test_get_principal_fails_closed_no_token(monkeypatch) -> None:
    """require_auth=True + no token ⇒ 401, before any DB lookup."""
    from fastapi import HTTPException

    monkeypatch.setattr(auth_mod, "get_settings", lambda: _FakeSettings(require_auth=True))
    monkeypatch.setattr(
        auth_mod,
        "session_scope",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no DB read when no token")),
    )
    with pytest.raises(HTTPException) as ei:
        asyncio.run(get_principal(_FakeRequest()))
    assert ei.value.status_code == 401


def test_get_principal_fails_closed_no_request(monkeypatch) -> None:
    """require_auth=True + request=None (no transport) ⇒ 401, never a principal."""
    from fastapi import HTTPException

    monkeypatch.setattr(auth_mod, "get_settings", lambda: _FakeSettings(require_auth=True))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(get_principal(None))
    assert ei.value.status_code == 401


def test_bearer_header_is_read(monkeypatch) -> None:
    """The Authorization: Bearer header is a valid token transport — an invalid
    bearer still reaches the (would-be) DB lookup, proving extraction works.
    We stub session_scope to assert the token was extracted (not a 'no token')."""
    seen = {}

    monkeypatch.setattr(auth_mod, "get_settings", lambda: _FakeSettings(require_auth=True))

    def _boom(*a, **k):
        seen["called"] = True
        raise RuntimeError("stop after extraction")

    monkeypatch.setattr(auth_mod, "session_scope", _boom)
    req = _FakeRequest(headers={"authorization": "Bearer abc123"})
    with pytest.raises(RuntimeError):
        asyncio.run(get_principal(req))
    assert seen.get("called"), "Bearer token was not extracted → DB lookup never reached"


# ── password hashing (scrypt) ────────────────────────────────────────────────
def test_scrypt_round_trip_and_constant_time_verify() -> None:
    h = _hash_password("correct horse battery staple")
    assert h.startswith("scrypt$"), "hash must carry the swappable algorithm tag"
    assert _verify_password("correct horse battery staple", h) is True
    assert _verify_password("wrong password", h) is False


def test_verify_password_handles_null_and_malformed() -> None:
    # NULL password_hash (SSO user) ⇒ clean False, never an exception.
    assert _verify_password("anything", None) is False
    assert _verify_password("anything", "") is False
    # Malformed / foreign-format hashes ⇒ False, not a crash.
    assert _verify_password("x", "argon2id$notyet") is False
    assert _verify_password("x", "scrypt$bad$format") is False


def test_hash_token_is_sha256_hex() -> None:
    import hashlib

    tok = "opaque-token"
    assert hash_token(tok) == hashlib.sha256(tok.encode()).hexdigest()
    assert len(hash_token(tok)) == 64


def test_two_hashes_of_same_password_differ_salt() -> None:
    """Distinct salts ⇒ distinct stored hashes for the same password."""
    a = _hash_password("pw")
    b = _hash_password("pw")
    assert a != b
    assert _verify_password("pw", a) and _verify_password("pw", b)


# ── projects.py no longer mints a random org via uuid4 ───────────────────────
def test_projects_does_not_use_uuid4_for_org() -> None:
    """The org_id source is the Principal, not a per-request uuid4."""
    src = (
        Path(__file__).resolve().parent.parent / "src" / "regsuper" / "api" / "projects.py"
    ).read_text(encoding="utf-8")
    assert "uuid4()" not in src, "projects.py must not synthesise a random org with uuid4()"
    assert "import uuid4" not in src and ", uuid4" not in src, (
        "the now-unused uuid4 import should be dropped"
    )
    assert "get_principal" in src, "projects.py must derive the org from get_principal"
    assert "client_id=p.org_id" in src, (
        "post_project must pass the principal org into create_project"
    )


def test_l0_create_project_writes_org_id_column() -> None:
    """create_project keeps the back-compat client_id= kwarg but writes the
    renamed org_id column (006 rename)."""
    src = (
        Path(__file__).resolve().parent.parent / "src" / "regsuper" / "analysis" / "l0_intake.py"
    ).read_text(encoding="utf-8")
    assert "org_id=client_id" in src, "create_project must write Project.org_id, not client_id"
    assert "client_id=client_id" not in src, "the broken client_id= ORM kwarg must be gone"


# ── real-DB session resolution (scratch-or-skip) ─────────────────────────────
def _scratch_dsn() -> str | None:
    """Reuse the migration-chain scratch-DB guard so we never touch real data."""
    try:
        from tests.test_migration_chain import _scratch_dsn as _msd
    except Exception:
        return None
    return _msd()


@pytest.mark.asyncio
async def test_live_session_resolves_principal(monkeypatch) -> None:
    """End-to-end on a scratch DB: a real UserSession → user + active org →
    Principal under require_auth=True. Skips when no scratch DB is configured."""
    dsn = _scratch_dsn()
    if dsn is None:
        pytest.skip("no scratch DB configured (see tests/test_migration_chain.py)")

    from datetime import datetime, timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from regsuper.storage.models import Membership, Organization, User, UserSession
    from scripts import apply_migrations as drv

    engine = create_async_engine(drv.to_asyncpg_url(dsn), future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    raw_token = "live-test-token"
    try:
        async with factory() as s:  # type: AsyncSession
            org = Organization(name="Scratch Org", org_type="sme")
            user = User(
                email="scratch@example.com", password_hash=_hash_password("pw"), status="active"
            )
            s.add(org)
            s.add(user)
            await s.flush()
            s.add(Membership(user_id=user.id, org_id=org.id, role="owner", seat_state="active"))
            s.add(
                UserSession(
                    user_id=user.id,
                    token_hash=hash_token(raw_token),
                    active_org_id=org.id,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            await s.commit()
            org_id, user_id = org.id, user.id

        # Point get_principal's session_scope at the scratch DB + flip the flag.
        monkeypatch.setattr(auth_mod, "get_settings", lambda: _FakeSettings(require_auth=True))
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _scope():
            async with factory() as s:
                yield s

        monkeypatch.setattr(auth_mod, "session_scope", _scope)
        p = await get_principal(_FakeRequest(cookies={auth_mod.SESSION_COOKIE: raw_token}))
        assert p.user_id == user_id
        assert p.org_id == org_id
        assert p.role == "owner"
    finally:
        await engine.dispose()
