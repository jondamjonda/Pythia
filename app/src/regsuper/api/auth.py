"""Product authentication and the request principal.

`get_principal` is the single source of the request's `org_id`, which tenancy uses for row-level
security (`SET LOCAL app.current_org`). With `require_auth=False` (local dev) it returns an
anonymous principal without touching the database; with `require_auth=True` it fails closed.
The /api/auth endpoints return 404 unless auth is required.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select

from regsuper.config import get_settings
from regsuper.storage.database import session_scope

# The ORM session model is `UserSession`; never import it as `Session`, which is easily confused
# with `sqlalchemy.orm.Session`.
from regsuper.storage.models import Membership, Organization, User, UserSession

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Sentinel ids used by the anonymous principal when require_auth=False.
#   * org: the sentinel org that migration 007_tenancy_backfill.sql attached to every row created
#     before tenancy. It is a real `organization` row, so the `project.org_id` foreign key resolves.
#     It must match that migration so local projects share the legacy tenant.
#   * user: all zeros, recognisably anonymous; no `user` row is needed because the anonymous
#     principal never touches the database.
_ANON_USER_ID = UUID("00000000-0000-0000-0000-000000000000")
_ANON_ORG_ID = UUID("00000000-0000-0000-0000-000000000001")

# Cookie that carries the opaque session token for browsers. API clients may send the token in an
# `Authorization: Bearer` header instead.
SESSION_COOKIE = "regsuper_session"

# Lifetime of a newly created server-side session.
_SESSION_TTL = timedelta(days=14)

# scrypt cost parameters (stdlib), sized for interactive logins. See _hash_password for how the
# hash format allows a later move to Argon2id.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16


@dataclass(frozen=True)
class Principal:
    """The authenticated identity resolved from a server-side session.

    Built from the session row's user, active org and role. `org_id` is the value
    `TenancyMiddleware` publishes for `SET LOCAL app.current_org` (row-level security).
    `is_platform_admin` grants control-room access across orgs.
    """

    user_id: UUID
    org_id: UUID
    role: str = "owner"
    is_platform_admin: bool = False


def anonymous_principal() -> Principal:
    """Return the anonymous principal used when `require_auth=False` (local dev only)."""
    return Principal(user_id=_ANON_USER_ID, org_id=_ANON_ORG_ID, role="owner")


# ---------------------------------------------------------------------------
# Token + password primitives
# ---------------------------------------------------------------------------
def _new_token() -> str:
    """Return a fresh opaque session token.

    The raw token goes to the client; only its SHA-256 is stored (`UserSession.token_hash`).
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of a session token, as stored in `UserSession.token_hash`.

    Sessions are looked up by this digest, never by the raw token, so a database leak does not
    expose live session tokens.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_password(password: str) -> str:
    """Salted password hash using stdlib `hashlib.scrypt`.

    Format: ``scrypt$<N>$<r>$<p>$<salt_hex>$<dk_hex>``. The leading algorithm tag keeps the
    format forward-compatible: a future ``argon2id$...`` tag can be dispatched on in
    `_verify_password` while existing scrypt hashes keep verifying (no forced password reset).
    """
    salt = os.urandom(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verify of `password` against a stored hash.

    Returns False (never raises) for a NULL (for example SSO-only) or malformed hash, so a
    missing credential is a clean authentication failure rather than a 500.
    """
    if not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        _, n_s, r_s, p_s, salt_hex, dk_hex = parts
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
    )
    # Constant-time comparison: an early-exit compare would leak the hash through timing.
    return hmac.compare_digest(candidate, expected)


def _extract_token(request: Request | None) -> str | None:
    """Return the session token from the cookie, else from an ``Authorization: Bearer`` header.

    Returns None if neither is present.
    """
    if request is None:
        return None
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        return tok
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


# ---------------------------------------------------------------------------
# The principal dependency: the single source of org_id for row-level security
# ---------------------------------------------------------------------------
async def get_principal(request: Request | None = None) -> Principal:
    """Resolve the request's `Principal`, the source of org_id for row-level security.

    With `require_auth=False` (the default, local dev only) this returns `anonymous_principal()`
    without reading the database and never denies. With `require_auth=True` it fails closed: the
    token's SHA-256 is looked up in `UserSession`, then the user and their active org (validated
    against `Membership`) are loaded. Any missing, expired, revoked or invalid session raises 401;
    a forged or partially populated principal is never returned.
    """
    settings = get_settings()
    if not settings.require_auth:
        return anonymous_principal()

    # Auth required: fail closed from here on.
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    token_hash = hash_token(token)

    async with session_scope() as session:
        sess = (
            await session.execute(select(UserSession).where(UserSession.token_hash == token_hash))
        ).scalar_one_or_none()
        if sess is None:
            raise HTTPException(status_code=401, detail="invalid session")
        now = datetime.now(UTC)
        if sess.revoked_at is not None or sess.expires_at <= now:
            raise HTTPException(status_code=401, detail="session expired")

        user = (
            await session.execute(select(User).where(User.id == sess.user_id))
        ).scalar_one_or_none()
        if user is None or user.status != "active":
            raise HTTPException(status_code=401, detail="invalid session")

        # Active org: the one pinned on the session, else the user's first active membership.
        org_id = sess.active_org_id
        membership = None
        if org_id is not None:
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.user_id == user.id,
                        Membership.org_id == org_id,
                    )
                )
            ).scalar_one_or_none()
        if membership is None:
            # No valid active org pinned on the session: fall back to the user's memberships.
            memberships = (
                (await session.execute(select(Membership).where(Membership.user_id == user.id)))
                .scalars()
                .all()
            )
            active = [m for m in memberships if m.seat_state == "active"]
            if not active:
                # A real session whose membership was revoked: fail closed rather than
                # silently widening scope.
                raise HTTPException(status_code=401, detail="no active membership")
            membership = active[0]
            org_id = membership.org_id

        return Principal(
            user_id=user.id,
            org_id=org_id,
            role=membership.role,
            is_platform_admin=bool(user.is_platform_admin),
        )


# ---------------------------------------------------------------------------
# Request/response shapes
# ---------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str
    password: str
    org_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class SwitchOrgRequest(BaseModel):
    org_id: UUID


class PrincipalResponse(BaseModel):
    user_id: UUID
    org_id: UUID
    role: str
    is_platform_admin: bool


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the session token as an HttpOnly, SameSite=Lax cookie.

    The Secure attribute is left to the edge proxy, because local dev runs over plain HTTP.
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=int(_SESSION_TTL.total_seconds()),
        path="/",
    )


async def _open_session(
    session,
    *,
    user_id: UUID,
    active_org_id: UUID | None,
    request: Request | None,
) -> str:
    """Create a server-side `UserSession` and return the raw token; only its SHA-256 is stored.

    The caller sets the cookie and commits.
    """
    token = _new_token()
    row = UserSession(
        user_id=user_id,
        token_hash=hash_token(token),
        active_org_id=active_org_id,
        expires_at=datetime.now(UTC) + _SESSION_TTL,
        ip=(request.client.host if request and request.client else None),
        user_agent=(request.headers.get("user-agent") if request else None),
    )
    session.add(row)
    return token


@router.post("/register", response_model=PrincipalResponse)
async def register(req: RegisterRequest, request: Request, response: Response) -> PrincipalResponse:
    """Sign up: create a user, their organization and an owner membership, and open a session.

    The session token is returned in an HttpOnly cookie. Returns 409 if the email is already
    registered, and 404 when authentication is disabled.
    """
    settings = get_settings()
    if not settings.require_auth:
        raise HTTPException(status_code=404, detail="auth disabled (require_auth=False)")
    async with session_scope() as session:
        existing = (
            await session.execute(select(User).where(User.email == req.email))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=409, detail="email already registered")
        user = User(
            email=req.email,
            password_hash=_hash_password(req.password),
            status="active",
        )
        org = Organization(name=req.org_name or req.email, org_type="sme")
        session.add(user)
        session.add(org)
        await session.flush()
        membership = Membership(user_id=user.id, org_id=org.id, role="owner", seat_state="active")
        session.add(membership)
        token = await _open_session(session, user_id=user.id, active_org_id=org.id, request=request)
        principal = PrincipalResponse(
            user_id=user.id,
            org_id=org.id,
            role="owner",
            is_platform_admin=bool(user.is_platform_admin),
        )
    _set_session_cookie(response, token)
    return principal


@router.post("/login", response_model=PrincipalResponse)
async def login(req: LoginRequest, request: Request, response: Response) -> PrincipalResponse:
    """Log in with email and password and open a session (token in an HttpOnly cookie).

    A wrong email and a wrong password both return the same 401, so the endpoint cannot be used
    to discover registered emails. Returns 403 if the user has no active membership, and 404
    when authentication is disabled.
    """
    settings = get_settings()
    if not settings.require_auth:
        raise HTTPException(status_code=404, detail="auth disabled (require_auth=False)")
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.email == req.email))
        ).scalar_one_or_none()
        if user is None or not _verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="invalid credentials")
        memberships = (
            (await session.execute(select(Membership).where(Membership.user_id == user.id)))
            .scalars()
            .all()
        )
        active = [m for m in memberships if m.seat_state == "active"]
        if not active:
            raise HTTPException(status_code=403, detail="no active membership")
        membership = active[0]
        user.last_login_at = datetime.now(UTC)
        token = await _open_session(
            session, user_id=user.id, active_org_id=membership.org_id, request=request
        )
        principal = PrincipalResponse(
            user_id=user.id,
            org_id=membership.org_id,
            role=membership.role,
            is_platform_admin=bool(user.is_platform_admin),
        )
    _set_session_cookie(response, token)
    return principal


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    """Revoke the current session and clear the cookie.

    Idempotent: a missing or invalid token still returns 204. Returns 404 when authentication
    is disabled.
    """
    settings = get_settings()
    if not settings.require_auth:
        raise HTTPException(status_code=404, detail="auth disabled (require_auth=False)")
    token = _extract_token(request)
    if token:
        async with session_scope() as session:
            sess = (
                await session.execute(
                    select(UserSession).where(UserSession.token_hash == hash_token(token))
                )
            ).scalar_one_or_none()
            if sess is not None and sess.revoked_at is None:
                sess.revoked_at = datetime.now(UTC)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/switch-org", response_model=PrincipalResponse)
async def switch_org(req: SwitchOrgRequest, request: Request) -> PrincipalResponse:
    """Switch the session's active organization (for example, an advisor acting for a client).

    The target must be one of the user's active memberships, otherwise 403. Returns 401 without
    a valid session, and 404 when authentication is disabled.
    """
    settings = get_settings()
    if not settings.require_auth:
        raise HTTPException(status_code=404, detail="auth disabled (require_auth=False)")
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")
    async with session_scope() as session:
        sess = (
            await session.execute(
                select(UserSession).where(UserSession.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if sess is None or sess.revoked_at is not None or sess.expires_at <= now:
            raise HTTPException(status_code=401, detail="invalid session")
        membership = (
            await session.execute(
                select(Membership).where(
                    Membership.user_id == sess.user_id,
                    Membership.org_id == req.org_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None or membership.seat_state != "active":
            raise HTTPException(status_code=403, detail="not a member of target org")
        sess.active_org_id = req.org_id
        user = (
            await session.execute(select(User).where(User.id == sess.user_id))
        ).scalar_one_or_none()
        return PrincipalResponse(
            user_id=sess.user_id,
            org_id=req.org_id,
            role=membership.role,
            is_platform_admin=bool(user.is_platform_admin) if user else False,
        )


@router.get("/me", response_model=PrincipalResponse)
async def me(request: Request) -> PrincipalResponse:
    """Return the current principal. Returns 404 when authentication is disabled."""
    settings = get_settings()
    if not settings.require_auth:
        raise HTTPException(status_code=404, detail="auth disabled (require_auth=False)")
    p = await get_principal(request)
    return PrincipalResponse(
        user_id=p.user_id,
        org_id=p.org_id,
        role=p.role,
        is_platform_admin=p.is_platform_admin,
    )
