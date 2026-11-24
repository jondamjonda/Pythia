"""HTTP middleware: per-request audit logging and tenancy context.

`AuditMiddleware` writes one audit row per request. `TenancyMiddleware` publishes the request's
org so database sessions can apply row-level security.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from regsuper.config import get_settings
from regsuper.observability.logging_config import get_logger
from regsuper.storage.audit import log_event
from regsuper.storage.database import (
    current_org_var,
    is_platform_admin_var,
    session_scope,
)

_log = get_logger(__name__)

# High-volume endpoints with no value in the audit log.
_SKIP_PATHS = {"/health", "/docs", "/openapi.json", "/favicon.ico"}

# When False (the default) the audit row is awaited inline, so the http.request row is written
# before the request completes. When True the same write is scheduled with asyncio.create_task,
# taking audit latency off the request's critical path. Both paths handle a failed write the same
# way (logged as audit_write_failed).
_AUDIT_FIRE_AND_FORGET = False

# Strong references to in-flight fire-and-forget audit writes (asyncio keeps only weak ones).
_background_tasks: set[asyncio.Task] = set()


async def _write_audit_event(
    *,
    request_id: str,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    client: str | None,
    error: str | None,
    org_id: str | None,
) -> None:
    """Write the http.request audit row for one request.

    Shared by the awaited and fire-and-forget paths, so the insert, the org_id stamp and the
    error handling are the same either way. Insert only: the audit table is append-only
    (enforced by the trg_audit_event_no_update trigger).
    """
    try:
        async with session_scope() as session:
            await log_event(
                session,
                actor_type="system",
                actor_id="http",
                action="http.request",
                target_type="endpoint",
                target_id=f"{method} {path}",
                payload={
                    "request_id": request_id,
                    "status": status,
                    "duration_ms": duration_ms,
                    "client": client,
                    "error": error,
                },
                org_id=org_id,
            )
    except Exception as exc:
        # An audit failure must not break the response: log it and move on.
        _log.error("audit_write_failed", error=repr(exc), request_id=request_id)


class AuditMiddleware(BaseHTTPMiddleware):
    """Write one audit row per request (method, path, status, duration, client, error)."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        response: Response | None = None
        error: str | None = None
        try:
            response = await call_next(request)
            return response
        except Exception as exc:
            error = repr(exc)
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            status = response.status_code if response is not None else 500

            # Stamp the org that TenancyMiddleware published, but only when tenancy is on.
            # Otherwise org_id stays None and the column is NULL.
            org_id: str | None = None
            if get_settings().tenancy_enabled:
                org_id = current_org_var.get()

            if _AUDIT_FIRE_AND_FORGET:
                # Schedule the write off the response path. The task carries the captured
                # values, so it is unaffected by TenancyMiddleware resetting the contextvar.
                task = asyncio.create_task(
                    _write_audit_event(
                        request_id=request_id,
                        method=request.method,
                        path=request.url.path,
                        status=status,
                        duration_ms=duration_ms,
                        client=request.client.host if request.client else None,
                        error=error,
                        org_id=org_id,
                    )
                )
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            else:
                # Default: write inline before the response completes.
                await _write_audit_event(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    duration_ms=duration_ms,
                    client=request.client.host if request.client else None,
                    error=error,
                    org_id=org_id,
                )


class TenancyMiddleware(BaseHTTPMiddleware):
    """Publish the request's org onto the tenancy contextvars used for row-level security.

    This is the reference implementation of the ``SET LOCAL app.current_org`` path (the conduit
    package mirrors it). It resolves the `Principal` once per request and publishes its `org_id`
    and `is_platform_admin` onto the shared contextvars in ``storage/database.py``. Every
    ``session_scope()`` or ``get_db()`` the handler opens reads them and sets the setting on its
    own connection; this middleware never opens a database session itself.

    When ``settings.tenancy_enabled`` is False (the default, and the only mode allowed locally)
    it is a pure pass-through: ``get_principal`` is not called and the contextvars keep their
    defaults, so ``_apply_org_guc`` issues nothing.

    When tenancy is enabled the contextvars are set before ``call_next`` and always restored with
    ``ContextVar.reset(token)`` in a ``finally``, so an org never leaks into the next request
    handled by the same worker.
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        # Tenancy off (default): pass through without resolving the principal.
        if not settings.tenancy_enabled:
            return await call_next(request)

        # auth.py is the single source of org_id. Imported lazily so the disabled path has no
        # import-time dependency on auth and there is no risk of an import cycle.
        from regsuper.api.auth import get_principal

        principal = await get_principal(request)
        # Keep it on request.state so later readers reuse the same principal without
        # resolving it (and querying the database) again.
        request.state.principal = principal

        org_token = current_org_var.set(str(principal.org_id))
        admin_token = is_platform_admin_var.set(bool(principal.is_platform_admin))
        try:
            return await call_next(request)
        finally:
            # Restore the on-entry values so nothing bleeds across requests on a reused worker.
            current_org_var.reset(org_token)
            is_platform_admin_var.reset(admin_token)
