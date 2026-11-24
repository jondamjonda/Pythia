"""FastAPI application entry point.

Wires together the routers, the middleware stack and the lifespan (connection pools and
background producers). `GET /health` is the readiness probe: it returns `{"status": "ok"}` when
the database is reachable.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from regsuper import __version__
from regsuper.analysis.llm_clients import close_openai, init_openai
from regsuper.api.admin import html_router as admin_html_router
from regsuper.api.admin import require_control_token
from regsuper.api.admin import router as admin_router
from regsuper.api.answer import router as answer_router
from regsuper.api.auth import router as auth_router
from regsuper.api.digest import router as digest_router
from regsuper.api.events import router as events_router
from regsuper.api.evidence import router as evidence_router
from regsuper.api.export import router as export_router
from regsuper.api.graph import router as graph_router
from regsuper.api.handoff import router as handoff_router
from regsuper.api.middleware import AuditMiddleware, TenancyMiddleware
from regsuper.api.municipalities import router as municipalities_router
from regsuper.api.profiles import router as profiles_router
from regsuper.api.projects import router as projects_router
from regsuper.api.provisions import router as provisions_router
from regsuper.api.pythia import router as pythia_router
from regsuper.api.retrieve import router as retrieve_router
from regsuper.api.trust import router as trust_router
from regsuper.config import get_settings, write_env_manifest
from regsuper.limits import validate_startup_limits
from regsuper.observability import (
    keys,
    nexus_poller,
    pythia_monitor,
    resource_guard,
    system_monitor,
)
from regsuper.observability.events import emit
from regsuper.observability.logging_config import configure_logging
from regsuper.pythia.client import close_pool, init_pool
from regsuper.storage.audit import audit_table_writeable_check
from regsuper.storage.database import dispose_engine, session_scope
from regsuper.storage.models import Finding

# The lifespan runs once per uvicorn/gunicorn worker. The system-stats collector, the resource
# watchdog, the Pythia monitor and the Nexus poller are single-producer background tasks (one
# psutil cpu_percent caller, one system_sample writer, one poll loop): running them in every worker
# would double-write rows, double-poll and corrupt the cpu_percent baseline. One "leader" worker is
# elected through a non-blocking exclusive file lock held for the process lifetime, and only the
# leader starts the producers. A single-worker run always wins the lock.
_leader_lock_fh: Any = None


def _acquire_leader_lock() -> bool:
    """Try to become this host's producer leader via a non-blocking exclusive flock.

    Returns True if this worker won the lock and should start the singleton producers. The file
    handle stays open for the process lifetime, because closing it would release the lock. On any
    platform or OS error this fails open (returns True) so a single-worker deployment never loses
    its producers.
    """
    global _leader_lock_fh
    try:
        import fcntl

        lock_path = get_settings().data_dir / "producer_leader.lock"
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False  # another worker holds the lock, so this one is a follower
        _leader_lock_fh = fh
        return True
    except Exception:
        return True


def _release_leader_lock() -> None:
    global _leader_lock_fh
    if _leader_lock_fh is not None:
        try:
            _leader_lock_fh.close()
        except Exception:
            pass
        _leader_lock_fh = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Refuse to boot a staging or prod deployment with product auth disabled. Local dev runs
    # without auth by default (require_auth is False there by design).
    _settings = get_settings()
    if not _settings.require_auth and _settings.environment in ("staging", "prod"):
        raise RuntimeError(
            f"refusing to boot: REQUIRE_AUTH is False in a {_settings.environment} environment "
            "(set REQUIRE_AUTH=true or change ENVIRONMENT)"
        )
    write_env_manifest(component="app")  # no-op unless AQ_ENV_MANIFEST_DIR is set
    await emit("server.started", version=__version__)
    keys.ensure_registered()  # seed the API-key registry so the budget grid has lines
    await validate_startup_limits()  # warns if the daily cap looks like a $1 test value
    await init_pool()  # pooled httpx.AsyncClient for the Pythia engine, created inside the loop
    await init_openai()  # process-wide AsyncOpenAI client, created inside the loop
    # Only the leader worker runs the single-producer background tasks (see _acquire_leader_lock).
    # Under `--workers N` exactly one worker starts them.
    _is_leader = _acquire_leader_lock()
    if _is_leader:
        await system_monitor.start()  # system-stats collector
        await resource_guard.start_watchdog()  # hard-cap watchdog; reads the stats ring buffer
        await pythia_monitor.start()  # forwards the Pythia engine's health to the event bus
        await nexus_poller.start()  # forwards Nexus's nexus_event store to the event bus
    else:
        from regsuper.observability.logging_config import get_logger as _get_logger

        _get_logger(__name__).info("producer_singletons_skipped_non_leader_worker", pid=os.getpid())
    yield
    await close_openai()  # drain and close the shared transport pools
    await close_pool()
    if _is_leader:
        await nexus_poller.stop()
        await pythia_monitor.stop()
        await resource_guard.stop_watchdog()
        await system_monitor.stop()
        _release_leader_lock()
    await emit("server.stopped", version=__version__)
    await dispose_engine()


app = FastAPI(
    title="Regulatory Super App",
    version=__version__,
    lifespan=lifespan,
)

# By default the local Next.js dev server may call the API. Ports 3000 and 3001 are both allowed
# because scripts/local/run_app_local.sh starts the dev UI on 3001 (3000 is often taken). In a real
# deployment, set FRONTEND_ORIGINS (comma-separated) to the deployed frontend's origins.
_default_origins = (
    "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001"
)
_origins = [
    o.strip() for o in os.environ.get("FRONTEND_ORIGINS", _default_origins).split(",") if o.strip()
]
# A wildcard origin combined with `allow_credentials=True` is unsafe: the CORS spec forbids
# `Access-Control-Allow-Origin: *` alongside credentials, and Starlette would instead echo the
# request Origin, letting any site make credentialed cross-origin calls. If FRONTEND_ORIGINS=*,
# keep the wildcard but drop credentials. With an explicit origin list, credentials stay enabled.
_allow_credentials = "*" not in _origins
if not _allow_credentials:
    import logging as _logging

    _logging.getLogger("regsuper").warning(
        "CORS configured with a wildcard origin (FRONTEND_ORIGINS=*); disabling "
        "allow_credentials to stay CORS-spec-compliant and avoid an any-origin credentialed hole"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditMiddleware)
# TenancyMiddleware publishes the request's org onto current_org_var so every session the handler
# opens sets the Postgres row-level-security setting. It is a no-op unless tenancy_enabled is True.
# The middleware added last runs outermost, so the contextvar is set before the handler (and before
# AuditMiddleware's finally block) runs, and reset afterwards.
app.add_middleware(TenancyMiddleware)
app.include_router(auth_router)  # product auth (/api/auth); 404 while require_auth is False
app.include_router(admin_html_router)  # static dashboard page shells (no data, no token needed)
app.include_router(admin_router)  # /admin data and control endpoints, gated by the control token
app.include_router(retrieve_router)
app.include_router(projects_router)
app.include_router(profiles_router)  # company profiles (404 until profiles_enabled)
app.include_router(municipalities_router)  # municipality autocomplete; authenticated
app.include_router(digest_router)  # plain-language digest (404 until digest_enabled)
app.include_router(provisions_router)
app.include_router(events_router)
app.include_router(pythia_router)  # proxy to the Pythia engine (off by default)
app.include_router(answer_router)  # POST /api/answer (404 until answer_endpoint_enabled)
app.include_router(handoff_router)  # referral to a lawful-lane provider (consent-gated PII)
app.include_router(export_router)  # audit export (404 until audit_export_enabled)
app.include_router(evidence_router)  # evidence pack (404 until audit_evidence_enabled)
app.include_router(trust_router)  # EU AI Act disclosures, human review, incident log
app.include_router(graph_router)  # act-graph diagram from the corpus DB; control-token gated


@app.get("/health")
async def health() -> dict[str, Any]:
    """Report database connectivity, the app version and whether the audit trigger is installed.

    Always answers 200; when the database is unreachable the body has `status: "error"` and the
    exception text in `detail`.
    """
    try:
        async with session_scope() as session:
            r = await session.execute(text("SELECT 1"))
            r.scalar_one()
            audit_trigger_ok = await audit_table_writeable_check(session)
        return {
            "status": "ok",
            "db": "connected",
            "version": __version__,
            "audit_trigger_installed": audit_trigger_ok,
        }
    except Exception as exc:
        return {"status": "error", "detail": repr(exc), "version": __version__}


# ---------------------------------------------------------------------------
# Internal test endpoints that verify citation enforcement and event wiring.
#
# They write to the database (a smoke audit row, a finding-insert attempt), so they are allowed only
# in local dev or with a valid control token: local and CI smoke tests keep working, while a
# non-local deployment never exposes them anonymously. This dependency is the only guard; there is
# no separate internal secret. The other /internal routes live in api/retrieve.py.
# ---------------------------------------------------------------------------
def _require_internal_access(x_control_token: str | None = Header(default=None)) -> None:
    """Allow the /internal/test/* routes in local dev; otherwise require the control token."""
    if get_settings().environment == "local":
        return
    require_control_token(x_control_token)


@app.post(
    "/internal/test/citation-empty",
    status_code=422,
    dependencies=[Depends(_require_internal_access)],
)
async def test_citation_empty() -> dict[str, str]:
    """Check that a finding without cited provisions is rejected.

    Inserts a finding with empty `cited_provisions`. Returns 422 with "cited_provisions cannot be
    empty" when the ORM insert hook rejects it, or 500 if the insert unexpectedly succeeds.
    """
    from regsuper.storage.audit import EmptyCitationError

    try:
        async with session_scope() as session:
            finding = Finding(
                project_id="00000000-0000-0000-0000-000000000000",
                finding_type="applicable",
                statement="test",
                severity="info",
                cited_provisions=[],
                cited_documents=[],
                confidence_overall=0.5,
                analysis_layer="L2",
                generated_by="test",
            )
            session.add(finding)
            await session.flush()
        raise HTTPException(500, "citation enforcement did not fire")
    except EmptyCitationError as exc:
        return {"error": "cited_provisions cannot be empty", "detail": str(exc)}


@app.post("/internal/test/emit-event", dependencies=[Depends(_require_internal_access)])
async def test_emit(code: str = "project.created", project_id: str = "test-project") -> dict:
    """Emit a test event and return it, to smoke-test the event pipeline."""
    evt = await emit(code, project_id=project_id, name="Smoke Test")
    return dict(evt)
