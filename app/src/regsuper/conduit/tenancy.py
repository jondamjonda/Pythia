"""Conduit tenancy: publish the caller's org so every transaction is scoped to it under RLS.

Conduit is a second process running under enforced row-level security, so it must stamp
`app.current_org` (derived by `auth.py` from the token or API key) as the first statement of
every request transaction, using the same transaction-local `set_config` pattern as the app.
Only org-scoped tables (`finding`, `project`, `billing_event`, `usage_counter`,
`conduit_credential`, ...) are filtered by it; the public corpus tables are not, and must not be
given RLS. This is what keeps a second internet-facing process from leaking data across tenants.

`ConduitTenancyMiddleware` publishes the request's org into the same `current_org_var` context
variable that `storage.database.session_scope` reads, so any session opened that way gets the
setting automatically. `set_current_org` is for a handler that stamps a session it opened
itself. Both pin `app.is_platform_admin` to off: a Conduit caller is never a platform admin.
Neither does anything when `rls_enforced` is off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

_WP = "WP-062 (Stage-2 Conduit build)"

if TYPE_CHECKING:  # FastAPI/SQLAlchemy types only needed for annotations
    from sqlalchemy.ext.asyncio import AsyncSession


class ConduitTenancyMiddleware:
    """ASGI middleware that publishes the request's org into the tenancy context variable.

    Mirrors the app's `TenancyMiddleware`, but takes the org from the `ConduitPrincipal`
    (token or API key) instead of a session cookie. For the duration of the request it sets
    `current_org_var` and pins `is_platform_admin_var` to False, so any session opened via
    `storage.database.session_scope` stamps `app.current_org` first when `rls_enforced` is on.
    The principal is read from `scope['state']['conduit_principal']`, set by the auth layer.
    Public routes (`/healthz`, the protected-resource metadata) carry no principal and run
    without an org, which is correct because they touch no tenant tables.

    The context variables are reset in a `finally`, so an org never carries over to another
    request or pooled connection.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Imported lazily so importing this module never pulls in the database engine.
        from regsuper.storage.database import current_org_var, is_platform_admin_var

        principal = self._principal_from_scope(scope)
        if principal is None or not getattr(principal, "org_id", None):
            # No authenticated principal (a public route, or auth left to the handler's
            # dependency): publish nothing, so no org setting is issued.
            await self.app(scope, receive, send)
            return

        org_token = current_org_var.set(str(principal.org_id))
        # A Conduit principal is never a platform admin.
        admin_token = is_platform_admin_var.set(False)
        try:
            await self.app(scope, receive, send)
        finally:
            current_org_var.reset(org_token)
            is_platform_admin_var.reset(admin_token)

    @staticmethod
    def _principal_from_scope(scope: Any) -> Any | None:
        """Read the resolved `ConduitPrincipal` from the ASGI scope state, if present.

        Starlette exposes `request.state` as `scope['state']`, where the auth layer stores
        `conduit_principal`. Returns None for a public or unauthenticated request.
        """
        state = scope.get("state")
        if isinstance(state, dict):
            return state.get("conduit_principal")
        return getattr(state, "conduit_principal", None) if state is not None else None


async def set_current_org(session: AsyncSession, org_id: str) -> None:
    """Stamp `org_id` into the session's transaction-local RLS settings.

    Mirrors `storage.database._apply_org_guc`. Does nothing when `rls_enforced` is off (the
    default). When it is on, issues these as the first statements of the transaction:

        SELECT set_config('app.current_org', :org, true)
        SELECT set_config('app.is_platform_admin', 'off', true)

    `set_config(..., is_local=true)` is a regular function and accepts bound parameters, unlike
    `SET LOCAL name = :v` ("syntax error at or near $1"). The org is therefore bound, with no
    SQL-injection surface, and the settings reset when the transaction ends, so nothing leaks
    across pooled connections. A Conduit caller is always `is_platform_admin='off'`.
    """
    from sqlalchemy import text as sql_text

    from regsuper.config import get_settings

    if not get_settings().rls_enforced:
        # RLS not enforced: nothing to stamp (same as _apply_org_guc).
        return
    await session.execute(
        sql_text("SELECT set_config('app.current_org', :org, true)"),
        {"org": str(org_id)},
    )
    await session.execute(
        sql_text("SELECT set_config('app.is_platform_admin', :adm, true)"),
        {"adm": "off"},
    )


__all__ = ["ConduitTenancyMiddleware", "set_current_org"]
