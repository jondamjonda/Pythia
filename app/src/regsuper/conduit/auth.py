"""Conduit authentication: OAuth 2.1 bearer tokens and API keys.

Conduit is an OAuth 2.1 resource server: it validates tokens but never issues them. The
authorization server is an external identity provider configured through
`conduit_oauth_issuer_url`, `conduit_oauth_jwks_url` and `conduit_resource_uri` (all unset by
default). The API-key channel (`conduit_credential` rows with kind 'api_key') works without
any authorization server, so a missing one never blocks the service.

Bearer token validation fails closed, in this order:

1. Verify the JWT signature against the authorization server's JWKS, plus `exp` and `iss`.
2. `aud` must equal the canonical resource (`conduit_resource_uri`), otherwise 401. This
   defends against confused-deputy attacks.
3. The `org_id` claim must be present, otherwise 401.
4. The token must hold every scope the tool requires, otherwise 403 (`require_scope`).
5. The resolved org goes to `tenancy.py` (for RLS) and to `meter.precheck`, which re-checks
   the entitlement and quota because a token can outlive a plan downgrade.

Tokens and API keys are never logged or stored; only the org id and a credential hash leave
this module. A 401 carries `WWW-Authenticate: Bearer resource_metadata=<URL>` so clients can
discover the authorization server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # FastAPI types only needed for annotations; runtime import is lazy.
    from fastapi import Request

_WP = "WP-062 (Stage-2 Conduit build)"

# Request header carrying the API key.
API_KEY_HEADER = "X-API-Key"
# Prefix the credential hash is namespaced under, matching the `conduit:<sha256(secret)[:12]>`
# shape stored in `conduit_credential.credential_id`.
_CREDENTIAL_PROVIDER = "conduit"
_CREDENTIAL_ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class ConduitPrincipal:
    """The resolved caller identity for one Conduit request (bearer token or API key).

    Carries only non-secret identifiers; the raw token or key is never stored here or logged.
    `org_id` scopes the request's database access, `scopes` is the granted scope set, and
    `credential_id` identifies the credential for metering and rate limiting.
    """

    org_id: str  # UUID string; becomes app.current_org
    credential_id: str  # 'conduit:<sha256(secret)[:12]>': a hash, never the secret
    scopes: frozenset[str] = field(default_factory=frozenset)
    auth_kind: str = "oauth"  # 'oauth' | 'api_key'


def _www_authenticate_header() -> dict[str, str]:
    """Build the `WWW-Authenticate` header for a 401, so clients can discover the auth server.

    The metadata URL is the canonical resource URI plus the RFC 9728 well-known path. When the
    resource URI is not configured, a bare `Bearer` challenge is sent rather than a malformed
    URL.
    """
    from regsuper.config import get_settings

    resource = get_settings().conduit_resource_uri
    if resource:
        prm = f"{resource.rstrip('/')}/.well-known/oauth-protected-resource"
        return {"WWW-Authenticate": f'Bearer resource_metadata="{prm}"'}
    return {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str) -> Exception:
    """Return a 401 with the `WWW-Authenticate` header. Never echoes the credential."""
    from fastapi import HTTPException

    return HTTPException(status_code=401, detail=detail, headers=_www_authenticate_header())


async def get_principal(request: Request) -> ConduitPrincipal:
    """FastAPI dependency that resolves the request's `ConduitPrincipal`.

    Tries the bearer token first (`validate_oauth_token`), then the API key
    (`validate_api_key`). Fails closed: a request with neither a valid token nor a valid key
    gets a 401 with the `WWW-Authenticate` header. The bearer path is tried only when an
    Authorization header is present and an authorization server is configured. Credentials are
    never logged.
    """
    # Bearer token first, but only if an authorization server is configured.
    auth_header = request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        from regsuper.config import get_settings

        if get_settings().conduit_oauth_issuer_url:
            bearer = auth_header[len("bearer ") :].strip()
            try:
                return await validate_oauth_token(bearer)
            except NotImplementedError:
                # Bearer validation is unavailable (no JWKS URL or no pyjwt): fall through
                # to the API key instead of failing with a 500.
                pass

    # API key, which works without an authorization server.
    api_key = request.headers.get(API_KEY_HEADER)
    if api_key:
        return await validate_api_key(api_key)

    # Neither credential present: fail closed with a 401.
    raise _unauthorized("missing or invalid credentials")


async def validate_oauth_token(bearer: str) -> ConduitPrincipal:
    """Validate an OAuth 2.1 bearer token (steps 1 to 3 above) and return its principal.

    Checks the JWKS signature, `exp` and `iss`; requires `aud` to equal `conduit_resource_uri`
    and an `org_id` claim (otherwise 401). The token's space-delimited `scope` claim is
    intersected with the recognised Conduit scopes. The per-tool scope check (step 4) is
    `require_scope`, a separate call made where the tool is known. The token is never logged.

    This path is unit-tested against a test JWKS but not yet exercised against a production
    authorization server. `pyjwt[crypto]` is an optional dependency (the `conduit` extra),
    imported lazily so the base install never needs it. When pyjwt is missing or no
    authorization server is configured, this raises `NotImplementedError` and `get_principal`
    falls back to the API key, so a base install can never accidentally accept a bearer token.
    """
    from regsuper.config import get_settings

    settings = get_settings()
    issuer = settings.conduit_oauth_issuer_url
    jwks_url = getattr(settings, "conduit_oauth_jwks_url", None)
    audience = settings.conduit_resource_uri
    if not issuer or not jwks_url:
        # No authorization server configured: signal "OAuth not available" so get_principal
        # falls back to the API key.
        raise NotImplementedError(_WP)

    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError as exc:  # pyjwt is the optional `conduit` extra
        # Without the extra, behave exactly like an unconfigured authorization server: fall
        # back to the API key rather than accepting a bearer token.
        raise NotImplementedError(_WP) from exc

    # Steps 1 and 2: signature, exp and iss; aud must equal the resource (otherwise 401).
    try:
        signing_key = PyJWKClient(jwks_url).get_signing_key_from_jwt(bearer)
        claims = jwt.decode(
            bearer,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=audience,  # rejects a foreign aud (confused-deputy defence)
            issuer=issuer,
            options={"require": ["exp", "aud", "iss"]},
        )
    except Exception as exc:
        # Never echo the token or the underlying jwt error, which could leak claims.
        raise _unauthorized("invalid bearer token") from exc

    # Step 3: the org_id claim must be present (otherwise 401).
    org_id = claims.get("org_id")
    if not org_id:
        raise _unauthorized("token missing org_id claim")

    # Granted scopes come from the space-delimited `scope` claim, restricted to recognised
    # Conduit scopes (fail closed: an unknown scope is dropped, never carried through).
    from regsuper.conduit.scopes import project_entitlement_scopes

    raw_scope = claims.get("scope") or ""
    token_scopes = set(raw_scope.split()) if isinstance(raw_scope, str) else set(raw_scope or [])
    scopes = frozenset(project_entitlement_scopes(token_scopes))

    return ConduitPrincipal(
        org_id=str(org_id),
        # OAuth principals are identified as conduit:org:<org> (there is no API-key hash).
        credential_id=f"conduit:org:{org_id}",
        scopes=scopes,
        auth_kind="oauth",
    )


def require_scope(principal: ConduitPrincipal, tool: str) -> None:
    """Require the principal to hold every scope `tool` needs (step 4); otherwise raise 403.

    Pure and fail closed: an unknown tool requires the full scope set, so it is unreachable
    without every scope. Raises an `HTTPException` with 403 when a required scope is missing
    and returns None when authorised. It is separate from token validation because it needs
    the tool, and is applied after the principal has been resolved.
    """
    from fastapi import HTTPException

    from regsuper.conduit.scopes import ConduitTool, scopes_for_tool
    from regsuper.entitlements import HTTP_FORBIDDEN

    try:
        tool_enum = ConduitTool(tool)
    except ValueError:
        # Unknown tool: require the full scope set (fail closed).
        from regsuper.conduit.scopes import ALL_SCOPES

        required = ALL_SCOPES
    else:
        required = scopes_for_tool(tool_enum)

    if not required <= principal.scopes:
        raise HTTPException(
            status_code=HTTP_FORBIDDEN,
            detail="token lacks the required scope for this tool",
        )


async def validate_api_key(api_key: str) -> ConduitPrincipal:
    """Validate an `X-API-Key` against `conduit_credential` and return its principal.

    Hashes the presented secret to `conduit:<sha256(secret)[:12]>` (via
    `observability/keys.py`, so there is one hash definition across the app), looks up the
    matching `conduit_credential` row, and resolves its org and scopes. The secret is never
    stored on the principal or logged; only its hash and the org id leave this function.

    Raises a 401 (with the `WWW-Authenticate` header) when the key is empty or unknown, or the
    credential is not active (for example revoked). The lookup is by hash and read-only.
    """
    from sqlalchemy import text as sql_text

    from regsuper.observability.keys import key_id_for
    from regsuper.storage.database import session_scope

    secret = (api_key or "").strip()
    if not secret:
        raise _unauthorized("missing API key")

    # Hash the secret to the stored credential id; from here on only the hash is used, for the
    # lookup, the principal and any log line.
    credential_id = key_id_for(_CREDENTIAL_PROVIDER, secret)

    sql = """
        SELECT org_id, credential_id, scopes, status
          FROM conduit_credential
         WHERE credential_id = :cid
         LIMIT 1
    """
    async with session_scope() as session:
        row = (await session.execute(sql_text(sql), {"cid": credential_id})).first()

    # Unknown hash, or a revoked or inactive credential: fail closed with a 401. The message is
    # the same in both cases so a caller cannot tell which applies.
    if row is None or row[3] != _CREDENTIAL_ACTIVE:
        raise _unauthorized("invalid or revoked API key")

    org_id, stored_cid, scopes_raw, _status = row[0], row[1], row[2], row[3]
    scopes = frozenset(scopes_raw or [])
    return ConduitPrincipal(
        org_id=str(org_id),
        credential_id=str(stored_cid),
        scopes=scopes,
        auth_kind="api_key",
    )


def protected_resource_metadata() -> dict[str, object]:
    """Return the RFC 9728 protected-resource metadata document.

    Served publicly at `GET /.well-known/oauth-protected-resource`. Contains `resource` (the
    canonical RFC 8707 audience, `conduit_resource_uri`), `authorization_servers` (the
    configured issuer, or an empty list when none is configured, so the OAuth flow cannot
    complete while API keys still work), `scopes_supported` (every Conduit scope) and
    `bearer_methods_supported`.

    Raises NotImplementedError only when the resource URI itself is not configured; `app.py`
    then serves a minimal `{"resource": None}` document instead.
    """
    from regsuper.conduit.scopes import ALL_SCOPES
    from regsuper.config import get_settings

    settings = get_settings()
    resource = settings.conduit_resource_uri
    if not resource:
        # No canonical resource configured: let app.py serve its minimal document.
        raise NotImplementedError(_WP)

    issuer = settings.conduit_oauth_issuer_url
    return {
        "resource": resource,
        # Empty until an authorization server is configured; never a made-up issuer.
        "authorization_servers": [issuer] if issuer else [],
        "scopes_supported": sorted(ALL_SCOPES),
        "bearer_methods_supported": ["header"],
    }


__all__ = [
    "API_KEY_HEADER",
    "ConduitPrincipal",
    "get_principal",
    "protected_resource_metadata",
    "require_scope",
    "validate_api_key",
    "validate_oauth_token",
]
