"""The Pythia HTTP sidecar: the engine served over FastAPI.

A thin transport layer so a host app can use Pythia over the network without importing the
package. It wraps the engine and the answer pipeline without modifying them. The main endpoints:

  GET  /health      readiness: database reachable, Ollama reachable (and the model name)
  POST /v1/retrieve LLM-free graph retrieval (no Ollama needed); body: PythiaQuery
  POST /v1/answer   full grounded, cited answer (decompose, retrieve, generate, verify; needs Ollama)

Responses are `PythiaAnswer` serialized to JSON (`as_of` dates as ISO strings). A refused answer is
a valid legal outcome, so it is HTTP 200, not an error. Further read endpoints (/v1/candidates,
/v1/provisions, /v1/act, /v1/rulings, ...) expose the knowledge graph through NexusPort.

Run from the engine directory with a single worker (the asyncpg pool is a per-process singleton):
  .venv/bin/python -m uvicorn pythia.service:app --host 127.0.0.1 --port 9000 --workers 1

Poll GET /health/live for liveness: it answers from memory and touches no pool or LLM, so it stays
responsive while a minutes-long /v1/answer occupies the single worker. GET /health is the readiness
check.

This is an operator-local sidecar, not a public service. Answer-quality feature flags default to
off; docs/FLAG_GATES.md (generated from pythia/flag_registry.py) lists the gate each one must pass
before it may be enabled in a serving environment.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from pythia import telemetry
from pythia.contract import PythiaAnswer, PythiaQuery
from pythia.db import close_pool, get_pool
from pythia.engine import PythiaEngine
from pythia.ingest import monitoring as _monitoring
from pythia.llm import OllamaLLMAdapter
from pythia.ports import NexusPort
from pythia.reasoning.answer import answer as _answer

_log = logging.getLogger("pythia.service")

# One NexusPort per process: it lazily caches the GNN retriever and reuses the global asyncpg pool.
# The sidecar is the remote side of NexusPort; the /v1/* read endpoints below project it over HTTP.
_nexus = NexusPort()


class AnswerRequest(BaseModel):
    """Request body for POST /v1/answer.

    `method` selects the graph expansion: "cte" (default), "ppr" or "gnn" (needs the [gnn] extra).
    `verify` controls the edge-assertion (faithfulness) pass: "off", "gated" (default, advisory) or
    "always".
    """

    query: str
    method: str = "cte"
    k: int = 12
    max_hops: int = 3
    verify: str = "gated"
    as_of: date | None = None  # point-in-time law; with as-of rendering on, a past date serves that version's text
    # Optional business-profile facts that the intake step uses to seed or override the facts it
    # extracts: a small whitelisted dict of known facts (legal_form, nace_code, derived_tags, size
    # bands, jurisdiction, the five compliance-gate booleans, municipality_code, preferred_language).
    # It is a typed field, not conversation history, so the model keeps `extra='forbid'` below and
    # requests stay single-shot (no history can be smuggled in). The app sends it only when its
    # answer_profile_context_enabled flag is on. Facts feed only the answer path and audit_trace,
    # never the app's cascade or candidates.
    project_context: dict | None = None

    model_config = ConfigDict(extra="forbid")


class CandidatesRequest(BaseModel):
    """Request body for POST /v1/candidates: metadata filters for candidate generation."""

    jurisdiction: list[str] | None = None
    legal_domain: list[str] | None = None
    legal_hierarchy: list[str] | None = None
    as_of: date | None = None
    limit: int = 200
    include_case_law: bool = False  # statutes only by default; never surface rulings as candidate law


class ProvisionsRequest(BaseModel):
    """Request body for POST /v1/provisions and POST /v1/importance: a list of provision ids."""

    ids: list[str] = []


class DecisionRequest(BaseModel):
    """Request body for POST /v1/monitoring/decision: approve or reject one discovery_queue row.

    `decision` must be "approved" or "rejected". Recording a decision never triggers ingestion.
    """

    queue_id: str
    decision: str
    note: str | None = None


class RunOnceRequest(BaseModel):
    """Request body for POST /v1/ingestion/run-once.

    `dry_run`: None (default) uses the configured auto_ingest_dry_run; true forces a discover-and-
    dedupe preview; false forces a live manual cycle.
    """

    dry_run: bool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from pythia.config import write_env_manifest

    write_env_manifest(component="engine")  # no-op unless AQ_ENV_MANIFEST_DIR is set
    # Log-only check that warns when a sub-flag is on while its master is off. Such a sub-flag is
    # already inert; this only makes the misconfiguration visible. Silent at the safe defaults.
    from pythia.flag_registry import run_subflag_validator

    run_subflag_validator()
    await get_pool()  # warm the process-global asyncpg pool on startup
    yield
    await close_pool()  # release the process-global pool on shutdown


app = FastAPI(title="Pythia sidecar", version="1", lifespan=lifespan)


@app.middleware("http")
async def _telemetry_envelope(request, call_next):
    """Assign a request id to every request and record its latency in the telemetry ring.

    This gives the app's Pythia panel per-path latency (the answer path can take 88-360 s) and an
    http_5xx count. If a route raises an unhandled exception, the full traceback and request id are
    logged at ERROR and a 500 is recorded before re-raising; otherwise Starlette would return a bare
    500 with no traceback in the log. The exception is re-raised unchanged, so the HTTP contract and
    FastAPI's own 500 handling are untouched.
    """
    import time as _time

    rid = telemetry.next_request_id()
    telemetry.request_id_var.set(rid)
    t0 = _time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        _log.exception("unhandled_error request_id=%s method=%s path=%s", rid, request.method, request.url.path)
        telemetry.record(
            kind="request",
            request_id=rid,
            path=request.url.path,
            method=request.method,
            http_status=500,
            total_ms=round((_time.perf_counter() - t0) * 1000, 1),
        )
        raise
    telemetry.record(
        kind="request",
        request_id=rid,
        path=request.url.path,
        method=request.method,
        http_status=response.status_code,
        total_ms=round((_time.perf_counter() - t0) * 1000, 1),
    )
    # Expose the request id so the app can join its cost records to the engine's audit_trace.
    response.headers["X-Pythia-Request-Id"] = rid
    return response


@app.get("/metrics")
async def metrics() -> dict:
    """Return aggregate sidecar telemetry for the app's Pythia panel.

    Includes the grounded-answer rate (GAR) with the verdict split, the method mix and per-path
    latency. LLM-free and instant.
    """
    return telemetry.metrics()


@app.get("/v1/telemetry")
async def telemetry_traces(limit: int = 50) -> list[dict]:
    """Return recent per-request traces (the request-inspector feed)."""
    return telemetry.traces(limit)


@app.get("/health/live")
async def health_live() -> dict:
    """Liveness probe: an always-fast 200 that touches nothing (no pool, database or Ollama).

    `/health` acquires a pool connection and pings Ollama, so while a minutes-long `/v1/answer` runs
    on the single worker (the asyncpg pool is a per-process singleton) it can be starved, and an
    orchestrator that treats a slow `/health` as dead would kill a healthy worker mid-answer. This
    probe answers from process memory with no I/O, so it stays responsive on a saturated worker.
    Supervisors should poll it for liveness and use `/health` for readiness (database and model
    reachability). More workers would also relieve the starvation, but this endpoint makes liveness
    explicit and testable regardless of the worker count.
    """
    return {"status": "alive"}


@app.get("/health")
async def health() -> dict:
    """Readiness probe: checks that the corpus database and Ollama are reachable.

    Returns 200 with status "ok", or "degraded" (still 200) if only Ollama is down; /v1/answer then
    refuses gracefully while /v1/retrieve keeps working. Returns 503 only if the database is
    unreachable. Because it touches the pool and Ollama it can be slow on a busy single worker; poll
    `/health/live` for liveness.
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            db_ok = (await conn.fetchval("SELECT 1")) == 1
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"database unreachable: {exc!r}")
    adapter = OllamaLLMAdapter()
    try:
        ollama_ok = await adapter.available()
    except Exception:  # noqa: BLE001
        ollama_ok = False
    return {
        "status": "ok" if ollama_ok else "degraded",
        "db_connected": db_ok,
        "ollama_available": ollama_ok,
        "model": adapter.model,
    }


@app.post("/v1/retrieve", response_model=PythiaAnswer)
async def retrieve(q: PythiaQuery, method: str = "cte") -> PythiaAnswer:
    """LLM-free graph retrieval.

    The `method` query parameter selects the graph expansion: "cte" (default), "ppr" or "gnn".
    """
    try:
        ans = await PythiaEngine().retrieve(q, method=method)
        at = ans.audit_trace or {}
        telemetry.record(
            kind="retrieve",
            path="/v1/retrieve",
            method=method,
            n_cites=len(ans.cited_provisions),
            gnn_degraded=bool(at.get("degraded") or at.get("gnn_degraded")),
        )
        ans.audit_trace["request_id"] = telemetry.request_id_var.get()  # join key for the app's cost records
        return ans
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"retrieve failed: {exc!r}")


@app.post("/v1/answer", response_model=PythiaAnswer)
async def answer(req: AnswerRequest) -> PythiaAnswer:
    """Full grounded, cited answer. Needs Ollama.

    Runs decomposition, retrieval, generation and verification; with a local model this can take
    minutes when the model is cold.
    """
    try:
        ans = await _answer(
            req.query,
            method=req.method,
            k=req.k,
            max_hops=req.max_hops,
            verify=req.verify,
            as_of=req.as_of,
            project_context=req.project_context,
        )
        at = ans.audit_trace or {}
        telemetry.record(
            kind="answer",
            path="/v1/answer",
            method=req.method,
            verdict=ans.verdict,
            refused=ans.refused,
            confidence=ans.confidence,
            n_cites=len(ans.cited_provisions),
            retried=bool(at.get("retried")),
            grounded=at.get("grounded_citations"),
        )
        # Stamp the request id on every return (ok, refuse and abstain all pass here), so the audit_trace,
        # the X-Pythia-Request-Id header and the app's cost record share the same join key.
        ans.audit_trace["request_id"] = telemetry.request_id_var.get()
        return ans
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"answer failed: {exc!r}")


# NexusPort read endpoints: query-agnostic knowledge access over HTTP.
# Errors are mapped deliberately: a malformed provision id is a 422 (client error), an unknown act a
# 404, and any other failure a 500 that carries only the exception type name, never its repr (which
# can leak SQL fragments or row data). The engine endpoints (/v1/retrieve, /v1/answer) keep the repr
# for operator debugging; this hardening covers the read endpoints the app and UI call directly.
def _is_uuid(s: str) -> bool:
    try:
        UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _require_uuids(ids: list[str], field: str = "ids") -> None:
    """Raise a 422 if any id is not a UUID (otherwise asyncpg fails later with an opaque cast error)."""
    bad = [i for i in ids if not _is_uuid(i)]
    if bad:
        raise HTTPException(422, f"{field}: not valid provision UUIDs: {bad[:5]}")


@app.post("/v1/candidates")
async def candidates(req: CandidatesRequest) -> list[dict]:
    """Return in-force provisions matching metadata filters, ranked by structural centrality.

    No query or embeddings are involved; this is the app's candidate-generation step. `as_of`
    applies the point-in-time mask.
    """
    try:
        rows = await _nexus.candidates(
            jurisdiction=req.jurisdiction,
            legal_domain=req.legal_domain,
            legal_hierarchy=req.legal_hierarchy,
            as_of=req.as_of,
            limit=req.limit,
            include_case_law=req.include_case_law,
        )
        return [asdict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"candidates failed ({type(exc).__name__})")


@app.post("/v1/provisions")
async def provisions(req: ProvisionsRequest) -> list[dict]:
    """Return metadata and trust facets for a list of provision ids, in input order."""
    _require_uuids(req.ids)
    try:
        return [asdict(r) for r in await _nexus.fetch_provisions(req.ids)]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"provisions failed ({type(exc).__name__})")


@app.post("/v1/importance")
async def importance(req: ProvisionsRequest) -> dict[str, dict]:
    """Return {id: {centrality, community}} for a list of provision ids.

    Centrality is a "these matter most" ranking prior; community is the regulatory regime the
    provision belongs to. Isolated or unscored ids are omitted (not an error).
    """
    _require_uuids(req.ids)
    try:
        return await _nexus.importance(req.ids)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"importance failed ({type(exc).__name__})")


@app.get("/v1/browse")
async def browse() -> dict:
    """Return the faceted browse index (facets, regimes, spine, catalogue) for the regulation explorer."""
    try:
        return await _nexus.browse()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"browse failed ({type(exc).__name__})")


@app.get("/v1/act/{external_id:path}")
async def act(external_id: str, as_of: date | None = None) -> dict:
    """Return one act's provision tree, nested by hierarchy and annotated with centrality.

    The `:path` converter is required because Czech act ids contain a slash (for example 240/2013),
    which a plain path parameter would 404 on. The optional `as_of` query parameter gives a
    point-in-time view; omit it for the whole act. An unknown act returns 404.
    """
    try:
        tree = await _nexus.act_tree(external_id, as_of=as_of)
        if isinstance(tree, dict) and "error" in tree:  # unknown act: 404, not a 200 with an error body
            raise HTTPException(404, tree["error"])
        return tree
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"act_tree failed ({type(exc).__name__})")


# Nexus control-room feeds (read-only): corpus statistics, events and health.
@app.get("/v1/nexus/stats")
async def nexus_stats_ep() -> dict:
    """Return corpus, graph, centrality, embedding-coverage, GNN and database-size statistics.

    Includes the graph_freshness headline metric. Cached for about 45 seconds; powers the Nexus
    panel and the System view.
    """
    try:
        return await _nexus.nexus_stats()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"nexus_stats failed ({type(exc).__name__})")


@app.get("/v1/nexus/events")
async def nexus_events_ep(limit: int = 50) -> list[dict]:
    """Return recent nexus_event rows (ingest, reindex, materialize, check): the ingestion-runs feed."""
    try:
        return await _nexus.nexus_events(limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"nexus_events failed ({type(exc).__name__})")


@app.get("/v1/nexus/health")
async def nexus_health_ep() -> dict:
    """Return a quick Nexus liveness and freshness status."""
    try:
        return await _nexus.nexus_health()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"nexus_health failed ({type(exc).__name__})")


@app.get("/v1/rulings/{provision_id}")
async def rulings(provision_id: str, as_of: date | None = None) -> list[dict]:
    """Return the rulings that engage a statute provision, for the opt-in case-law panel.

    Each ruling includes court, case number, title and date, so no second lookup is needed. `as_of`
    applies the point-in-time mask (default today).
    """
    if not _is_uuid(provision_id):
        raise HTTPException(422, "provision_id: not a valid provision UUID")
    try:
        return [asdict(r) for r in await _nexus.rulings_for(provision_id, as_of=as_of)]
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"rulings failed ({type(exc).__name__})")


# Monitoring: the scan log and the ingest-approval gate, intended to be proxied by the app's control
# room. The gate's hard rule lives in ingest/monitoring.py: /v1/monitoring/decision only records the
# operator's choice in the operational discovery_queue and can never trigger ingestion. No worker acts
# on approved rows yet, so the loop ends at "decision recorded".
@app.get("/v1/monitoring/scans")
async def monitoring_scans() -> dict:
    """Return the scan log for the monitoring UI. Read-only: no ingestion and no writes.

    Returns three lists, each capped at 100:

    - `discovery_queue`: detected acts that are pending, approved or rejected (an empty list if the
      watcher's discovery_queue table does not exist yet).
    - `recent_version_events`: real amendments (provision_version rows with version_no > 1) and
      document_version rows, newest first by detected_at.
    - `stale_signals`: recent nexus_event rows of kind gnn-stale or graph-changed.
    """
    try:
        return await _monitoring.scan_log()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"monitoring_scans failed ({type(exc).__name__})")


@app.post("/v1/monitoring/decision")
async def monitoring_decision(req: DecisionRequest) -> dict:
    """Record the operator's approve or reject decision on one discovery_queue row.

    Returns the updated row, or 404 if no row has that id.

    This endpoint must never ingest. It is the human-in-the-loop gate and only writes the decision
    (status, note, decided_at) to the operational discovery_queue table; it never touches a corpus
    table or calls discover(), amend(), reindex, embed or materialize. Approving an item is a note to
    a human, not a trigger: no worker acts on approved rows yet, so the loop ends here.
    """
    decision = (req.decision or "").strip().lower()
    if decision not in ("approved", "rejected"):
        raise HTTPException(422, "decision must be 'approved' or 'rejected'")
    try:
        row = await _monitoring.record_decision(req.queue_id, decision, note=req.note)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"monitoring_decision failed ({type(exc).__name__})")
    if row is None:
        raise HTTPException(404, "no discovery_queue row with that queue_id")
    return row


# Auto-ingestion endpoints: status, run-once and history. The app's token-gated /admin router proxies
# them and owns enable/disable itself: it writes data/AUTO_INGEST_ENABLED, which the engine only reads
# (the `enabled` field of the status). The response shapes are a contract shared with the app. Status
# and history are plain database reads (no torch, instant). Run-once only triggers the orchestrator,
# which enforces all the gating itself (on/off mirror, single-flight lock, kill switch, memory
# pre-check, and a stop-the-world halt if the defined-term count drops), so these endpoints add no
# ingestion logic of their own.
#
# The orchestrator and resource signals are imported lazily inside the handlers, so this module stays
# light and torch-free at import time; nothing here loads the connectors or the embedding model until a
# run-once fires, and even then the heavy loading happens inside run_cycle behind its memory pre-check.

_INGEST_DDL_DONE = False  # process-level latch: run the IF NOT EXISTS DDL once, on first use


async def _ensure_ingestion_ddl(conn) -> None:
    """Create the ingestion_run ledger table if it does not exist yet (once per process).

    The DDL is idempotent and cheap; the latch only avoids re-issuing it on every status or history
    poll.
    """
    global _INGEST_DDL_DONE
    if _INGEST_DDL_DONE:
        return
    from pythia.ingest.auto_ingest import _DDL

    await conn.execute(_DDL)
    _INGEST_DDL_DONE = True


# The ingestion_run columns exposed in the status endpoint's `last_run` object.
_LAST_RUN_COLS = (
    "run_id",
    "started_at",
    "finished_at",
    "mode",
    "n_discovered",
    "n_ingested",
    "n_amended",
    "n_embedded",
    "n_enriched",
    "n_errors",
    "halted",
    "halt_reason",
)


def _row_to_run(r) -> dict:
    """Map an ingestion_run row to the API's run dict (id becomes run_id, timestamps become ISO strings)."""
    d = dict(r)
    out = {"run_id": str(d.get("id")) if d.get("id") is not None else None}
    for k in ("started_at", "finished_at"):
        v = d.get(k)
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    for k in (
        "mode",
        "n_discovered",
        "n_ingested",
        "n_amended",
        "n_embedded",
        "n_enriched",
        "n_errors",
        "halted",
        "halt_reason",
        "defined_terms_before",
        "defined_terms_after",
        "detail",
    ):
        out[k] = d.get(k)
    return out


def _next_run_at(now: datetime | None = None) -> str | None:
    """Return the next scheduled auto-ingest time (ISO 8601) from the configured cron string.

    Avoids a croniter / APScheduler dependency, neither of which is in the engine environment. Only
    the simple forms are supported: a fixed minute and hour, with day-of-week either '*' (daily) or a
    single weekday 0-6 (the default is Sunday 04:30). Any other cron form returns None, meaning
    "unknown", rather than a wrong time. The result is a naive local-time timestamp, since the cron
    fields are local time (as in scheduler.py).
    """
    from pythia.config import get_settings

    spec = (getattr(get_settings(), "cron_auto_ingest", "") or "").split()
    if len(spec) != 5:
        return None
    minute_s, hour_s, dom_s, mon_s, dow_s = spec
    # Only the simple "every day/this weekday at HH:MM" forms are computed here.
    if dom_s != "*" or mon_s != "*":
        return None
    try:
        minute, hour = int(minute_s), int(hour_s)
    except ValueError:
        return None
    if not (0 <= minute < 60 and 0 <= hour < 24):
        return None
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dow_s == "*":  # daily: today or tomorrow at HH:MM
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()
    try:
        dow = int(dow_s)  # cron: 0 = Sunday ... 6 = Saturday
    except ValueError:
        return None
    if not (0 <= dow <= 6):
        return None
    py_target_wd = (dow - 1) % 7  # convert to Python's weekday(): Monday = 0 ... Sunday = 6
    days_ahead = (py_target_wd - now.weekday()) % 7
    target += timedelta(days=days_ahead)
    if target <= now:  # same weekday but the time has passed: next week
        target += timedelta(days=7)
    return target.isoformat()


async def _queue_pending(conn) -> int:
    """Return the number of pending discovery_queue rows (the auto-ingest backlog).

    Best-effort: returns 0 if the watcher never ran (no table), so the status read never fails.
    """
    try:
        return int(await conn.fetchval("SELECT count(*) FROM discovery_queue WHERE status = 'pending'") or 0)
    except Exception:  # noqa: BLE001 — table absent or schema differs: no backlog to report
        return 0


async def _recent_alarms(conn, limit: int = 20) -> list[str]:
    """Return recent auto-ingest alarm event kinds from nexus_event, newest first.

    Feeds the status endpoint's `alarms`. Best-effort: an unreadable event table gives [].
    """
    try:
        rows = await conn.fetch(
            "SELECT kind FROM nexus_event WHERE kind LIKE 'auto-ingest%' ORDER BY created_at DESC LIMIT $1", limit
        )
        return [r["kind"] for r in rows]
    except Exception:  # noqa: BLE001
        return []


@app.get("/v1/ingestion/status")
async def ingestion_status() -> dict:
    """Return the auto-ingestion status. A read-only call: no torch, instant.

    Fields:

    - `enabled`: the data/AUTO_INGEST_ENABLED switch written by the app (the engine only reads it).
    - `dry_run`: the configured auto_ingest_dry_run default.
    - `running`: whether a fresh single-flight lock is held (a cycle is in progress).
    - `last_run`: the latest ingestion_run row, or null.
    - `next_run_at`: the next scheduled run from the cron config, or null if it cannot be computed.
    - `queue_pending`: pending discovery_queue rows (the backlog).
    - `coverage_gap_in_force`: in-force provisions still missing an embedding.
    - `defined_terms_total`: the corpus-wide defined_term count (the baseline for the halt guard).
    - `alarms`: recent auto-ingest alarm event kinds.

    The on/off switch and the lock are read from data/ without the database. If the database read
    fails, the database-derived fields keep their placeholder values and `reachable` is false,
    instead of the whole call failing.
    """
    from pythia.config import get_settings
    from pythia.ingest import _resource_signals as rs
    from pythia.ingest import auto_ingest as ai

    s = get_settings()
    enabled = rs.auto_ingest_enabled_file()
    running = rs.is_running(lock_ttl_s=s.lock_ttl_s)
    out: dict = {
        "enabled": bool(enabled),
        "dry_run": bool(s.auto_ingest_dry_run),
        "running": bool(running),
        "last_run": None,
        "next_run_at": _next_run_at(),
        "queue_pending": 0,
        "coverage_gap_in_force": -1,
        "defined_terms_total": -1,
        "alarms": [],
    }
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await _ensure_ingestion_ddl(conn)
            r = await conn.fetchrow(
                "SELECT id, started_at, finished_at, mode, n_discovered, n_ingested, n_amended, "
                "n_embedded, n_enriched, n_errors, halted, halt_reason FROM ingestion_run "
                "ORDER BY started_at DESC LIMIT 1"
            )
            if r is not None:
                lr = _row_to_run(r)
                out["last_run"] = {k: lr.get(k) for k in _LAST_RUN_COLS}
            out["queue_pending"] = await _queue_pending(conn)
            out["coverage_gap_in_force"] = await ai._coverage_gap_in_force(conn)
            out["defined_terms_total"] = await ai._defined_terms_total(conn)
            out["alarms"] = await _recent_alarms(conn)
    except Exception as exc:  # noqa: BLE001 — flag and lock are already set; report a degraded read
        _log.warning("ingestion_status: DB-derived fields unavailable: %r", exc)
        out["reachable"] = False
    return out


@app.post("/v1/ingestion/run-once")
async def ingestion_run_once(req: RunOnceRequest) -> dict:
    """Start one auto-ingestion cycle in the background.

    Single-flight: if a fresh run lock is already held, returns 409 "already_running" and no second
    cycle starts. Otherwise the cycle is started as a background task and the call returns
    immediately with {started: true, run_id}. The cycle does all the gating itself (on/off switch,
    lock, kill switch, memory pre-check, defined-term halt guard); this endpoint only starts it.
    `dry_run` in the body overrides the configured default when set.

    The returned run_id is a correlation id minted here; the orchestrator assigns its own run id to
    the ingestion_run row, which appears in /v1/ingestion/status and /v1/ingestion/history once the
    cycle records it. run_cycle does not accept an external id, which keeps this module free of
    orchestration logic.
    """
    from pythia.config import get_settings
    from pythia.ingest import _resource_signals as rs
    from pythia.ingest.scheduler import run_auto_ingest

    s = get_settings()
    if rs.is_running(lock_ttl_s=s.lock_ttl_s):
        raise HTTPException(409, "already_running")

    import uuid as _uuid

    run_id = str(_uuid.uuid4())

    async def _bg() -> None:
        try:
            res = await run_auto_ingest(dry_run=req.dry_run)
            _log.info(
                "ingestion run-once (corr=%s) finished: %s",
                run_id,
                {k: res.get(k) for k in ("run_id", "mode", "halted", "halt_reason")},
            )
        except Exception:  # noqa: BLE001 — log a background crash; the run row records the failure
            _log.exception("ingestion run-once (corr=%s) raised", run_id)

    asyncio.create_task(_bg())
    return {"started": True, "run_id": run_id}


@app.get("/v1/ingestion/history")
async def ingestion_history(limit: int = 20) -> dict:
    """Return recent ingestion runs, newest first, as {runs: [...]}.

    Read-only. `limit` is clamped to 1-200. Each run has run_id, ISO timestamps, counts, halt
    fields, the defined-term counts before and after, and the detail JSON. The list is empty if no
    run has been recorded yet.
    """
    limit = max(1, min(int(limit), 200))
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await _ensure_ingestion_ddl(conn)
            rows = await conn.fetch(
                "SELECT id, started_at, finished_at, mode, n_discovered, n_ingested, n_amended, "
                "n_embedded, n_enriched, n_errors, halted, halt_reason, defined_terms_before, "
                "defined_terms_after, detail FROM ingestion_run ORDER BY started_at DESC LIMIT $1",
                limit,
            )
        return {"runs": [_row_to_run(r) for r in rows]}
    except Exception as exc:  # noqa: BLE001 — return an empty, degraded history rather than a 500
        _log.warning("ingestion_history unavailable: %r", exc)
        return {"runs": [], "reachable": False}
