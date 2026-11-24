"""Conduit ASGI app factory.

`create_conduit_app()` builds Conduit's own FastAPI app: the REST router (`rest.py`), the
public OAuth protected-resource metadata endpoint (RFC 9728), `/healthz`, and the MCP
Streamable HTTP sub-app (`mcp_server.py`) when the optional `mcp` SDK is installed. It never
mounts `/admin/*` or `/internal/*` routes, and it runs in a process entirely separate from
`api/main.py`; the main app must never import this module.

While `conduit_enabled` is off (the default) the factory refuses to build the app and raises
`ConduitDisabledError`, so the internet-facing service cannot start by accident. Without the
`mcp` SDK the MCP channel is simply not mounted; the API-key REST channel does not depend on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from regsuper.config import get_settings

if TYPE_CHECKING:  # never imported at runtime unless the factory is called
    from fastapi import FastAPI

_WP = "WP-062 (Stage-2 Conduit build)"


class ConduitDisabledError(RuntimeError):
    """Raised by `create_conduit_app` when `conduit_enabled` is off.

    Conduit must refuse to start its public listeners while the flag is off. This is a hard
    failure by design: the default is off, so a misconfigured deployment fails loudly instead
    of silently exposing an internet-facing service.
    """


def create_conduit_app(*, conduit_enabled: bool | None = None) -> FastAPI:
    """Build the Conduit ASGI app, or refuse when Conduit is disabled.

    Args:
        conduit_enabled: Override for the `conduit_enabled` setting (used by tests). When None,
            the configured value is used.

    Returns:
        A FastAPI app with the REST router, the protected-resource metadata endpoint,
        `/healthz` and, if the `mcp` SDK is installed, the MCP sub-app. No `/admin` or
        `/internal` routes are mounted.

    Raises:
        ConduitDisabledError: `conduit_enabled` resolves to False (the default).
        NotImplementedError: The `mcp` SDK is installed but the MCP registry is not
            implemented yet.
    """
    enabled = conduit_enabled if conduit_enabled is not None else get_settings().conduit_enabled
    if not enabled:
        # Refuse to start the public listeners while Conduit is disabled.
        raise ConduitDisabledError(
            "Conduit refuses to start: conduit_enabled=False (safe default). "
            "The Conduit distribution service is a separate internet-facing process "
            "and only starts when the kill-switch flag is explicitly enabled."
        )
    # Enabled: assemble the public app. FastAPI and the Conduit modules are imported here
    # rather than at module level so that importing `regsuper.conduit` stays side-effect-free.
    from fastapi import FastAPI

    from regsuper.conduit import auth as conduit_auth
    from regsuper.conduit.rest import router as rest_router

    app = FastAPI(
        title="Polarius Conduit",
        description=(
            "Curated, read-only, information-lane distribution of the CZ/EU "
            "legislation+obligations corpus (MCP + REST). General legal information, "
            "not legal advice."
        ),
        # No interactive docs on the internet-facing surface by default.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # The REST channel, one endpoint per tool. The router has no routes yet; mounting it now
    # keeps the route table complete once the handlers are registered.
    app.include_router(rest_router)

    # Public, unauthenticated endpoints: the RFC 9728 protected-resource metadata and liveness.
    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    def _protected_resource_metadata() -> dict[str, object]:
        # auth.protected_resource_metadata builds the document from settings and raises
        # NotImplementedError when the resource URI is not configured. Serve a minimal
        # document instead of a 500; the API-key channel works without an OAuth server.
        try:
            return conduit_auth.protected_resource_metadata()
        except NotImplementedError:
            return {"resource": get_settings().conduit_resource_uri}

    @app.get("/healthz", include_in_schema=False)
    def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    # Mount the MCP Streamable HTTP sub-app if the optional `mcp` SDK is installed; without it
    # the REST channel still comes up. No /admin or /internal route is mounted either way.
    _mount_mcp_subapp(app)

    return app


def _mount_mcp_subapp(app: Any) -> None:
    """Mount the MCP Streamable HTTP sub-app at `/mcp` when the `mcp` SDK is installed.

    The SDK is an optional dependency (the `conduit` extra), so its import is guarded here
    rather than at module level. Without it the MCP channel is not mounted and the REST
    channel, metadata and health endpoints still serve.
    """
    try:
        import mcp  # noqa: F401  (presence check only)
    except ImportError:
        return
    # The SDK is present: build and mount the registry. `build_mcp_server` is not implemented
    # yet and raises NotImplementedError; letting that propagate is deliberate ("MCP was
    # requested but is not wired yet") rather than a silent no-op.
    from regsuper.conduit.mcp_server import build_mcp_server

    server = build_mcp_server()
    # The sub-app accessor (streamable_http_app()) depends on the SDK version, so check for it
    # instead of assuming it exists.
    sub_app = getattr(server, "streamable_http_app", None)
    if callable(sub_app):
        app.mount("/mcp", sub_app())


def _build_middleware_stack(app: Any) -> None:
    """Install the Conduit middleware chain: auth, tenancy, rate limit, then meter precheck.

    Not implemented yet. Like the rest of the app, it must not add any /admin or /internal
    route.
    """
    raise NotImplementedError(_WP)


__all__ = ["ConduitDisabledError", "create_conduit_app"]
