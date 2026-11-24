"""Admin and control-room endpoints: ČNB PDF ingestion, platform observability and safety controls.

A lawyer hand-picks the ČNB (Czech National Bank) methodologies to ingest, because choosing them
takes legal judgement; uploads land in ``${DATA_DIR}/cnb_pdfs/`` and are ingested synchronously so
the user sees the result at once. The rest of the module feeds the operator's control room:
system metrics, spend, kill switch, limits, module panels and the event stream.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select, text
from sse_starlette.sse import EventSourceResponse

from regsuper.analysis import transposition_map
from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.ingest.cnb import discover_pdfs, ingest_pdf
from regsuper.observability import (
    api_tracker,
    call_stats,
    cascade_tracker,
    db_schema,
    keys,
    killswitch,
    limits,
    metrics,
    resource_guard,
    system_monitor,
)
from regsuper.observability.events import (
    SYSTEM_CHANNEL,
    Event,
    emit,
    replay_recent,
    subscribe,
    unsubscribe,
)
from regsuper.observability.logging_config import get_logger
from regsuper.pythia import client as pythia_client
from regsuper.storage.database import session_scope
from regsuper.storage.models import (
    ApiCall,
    DefinedTerm,
    Provision,
    ProvisionSentence,
    SourceDocument,
)

_log = get_logger(__name__)


def require_control_token(x_control_token: str | None = Header(default=None)) -> None:
    """Require the control-room token on /admin endpoints.

    If CONTROL_PANEL_TOKEN is configured, requests must carry a matching X-Control-Token header
    (the Next.js server proxy forwards it), otherwise 401. The comparison is constant-time
    (``secrets.compare_digest``), so a wrong token cannot be recovered byte by byte through timing.

    When no token is configured the gate fails closed (503) outside local dev, unless the operator
    has explicitly opted into an open control room (``control_room_open_when_unset=True``). In
    local dev it stays open, because the loopback bind is the security boundary.
    """
    settings = get_settings()
    tok = settings.control_panel_token
    if not tok:
        # No token configured: local dev stays open (loopback boundary); anywhere else, refuse
        # unless the operator explicitly opted into an open control room.
        if settings.environment != "local" and not settings.control_room_open_when_unset:
            _log.warning("control_room_gate_closed_token_unset", environment=settings.environment)
            raise HTTPException(status_code=503, detail="control panel token not configured")
        return
    if x_control_token is None or not secrets.compare_digest(x_control_token, tok):
        raise HTTPException(status_code=401, detail="control panel token required")


# Every route on the /admin router requires the control token, including sensitive ones such as
# PDF ingestion (/cnb/upload), raw LLM prompts (/api/calls/{id}), the log tail (/api/logs) and
# re-ingestion (/cnb/reingest). In local dev with no token configured the gate stays open.
# The static HTML page shells (dashboard, uploader, schema, calls) are served without a token from
# a separate sub-router: a browser cannot attach the server-side X-Control-Token to a top-level
# navigation, so the shells must load unauthenticated. They carry no data and only fetch the gated
# JSON endpoints, which is where the data is protected.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_control_token)])

# Open sub-router for the static dashboard page shells only (no data, no side effects).
html_router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)


# ---------------------------------------------------------------------------
# Suggested ČNB methodologies: short labels for the upload checklist, which the lawyer ticks off
# as PDFs are dropped. The list is informational only and can be edited freely.
# ---------------------------------------------------------------------------
SUGGESTED_CNB: list[dict[str, str]] = [
    # Funds / asset management — directly relevant to the AIFM use case.
    {
        "label": "Úřední sdělení ČNB k AIFM – marketing zahraničních fondů (2018)",
        "hint": "us_2018_aifm_marketing",
    },
    {"label": "Úřední sdělení ČNB ke kapitálové přiměřenosti AIFM", "hint": "us_aifm_kapital"},
    {"label": "Úřední sdělení ČNB k delegaci činností u AIFM", "hint": "us_aifm_delegace"},
    {"label": "Úřední sdělení ČNB k pravidlům jednání správce fondu", "hint": "us_aifm_jednani"},
    {
        "label": "Úřední sdělení ČNB k odměňování (remuneration) – AIFMD/UCITS",
        "hint": "us_remuneration",
    },
    {"label": "Úřední sdělení ČNB k ESG / SFDR disclosures", "hint": "us_esg_sfdr"},
    # MiFID II / investment services
    {
        "label": "Úřední sdělení ČNB k provádění pokynů (best execution)",
        "hint": "us_best_execution",
    },
    {"label": "Úřední sdělení ČNB k pobídkám (inducements)", "hint": "us_pobidky"},
    {
        "label": "Úřední sdělení ČNB k testu vhodnosti a přiměřenosti (MiFID II)",
        "hint": "us_test_vhodnosti",
    },
    {"label": "Úřední sdělení ČNB k řízení střetu zájmů", "hint": "us_stret_zajmu"},
    {
        "label": "Úřední sdělení ČNB k product governance (MiFID II)",
        "hint": "us_product_governance",
    },
    # AML / KYC
    {"label": "Úřední sdělení ČNB k identifikaci a kontrole klienta (AML)", "hint": "us_aml_kyc"},
    {
        "label": "Úřední sdělení ČNB k systému vnitřních zásad podle AML zákona",
        "hint": "us_aml_vnitrni_zasady",
    },
    {"label": "Úřední sdělení ČNB k PEP (politicky exponované osoby)", "hint": "us_pep"},
    # Governance / internal control
    {
        "label": "Úřední sdělení ČNB k systému vnitřního řízení a kontroly",
        "hint": "us_vnitrni_rizeni",
    },
    {"label": "Úřední sdělení ČNB k outsourcingu", "hint": "us_outsourcing"},
    {"label": "Úřední sdělení ČNB k ICT a kyberbezpečnosti (DORA)", "hint": "us_dora"},
    {"label": "Úřední sdělení ČNB k whistleblowingu", "hint": "us_whistleblowing"},
    # Prudential / reporting
    {"label": "Úřední sdělení ČNB k reportingu (FINREP/COREP)", "hint": "us_reporting"},
    {
        "label": "Úřední sdělení ČNB ke kapitálovým požadavkům (CRR / CRD)",
        "hint": "us_kapital_pozadavky",
    },
    {"label": "Úřední sdělení ČNB k likviditě (LCR/NSFR)", "hint": "us_likvidita"},
    # Crypto / MiCA
    {"label": "Úřední sdělení ČNB k MiCA – tržní účastníci kryptoaktiv", "hint": "us_mica"},
    {
        "label": "Úřední sdělení ČNB k poskytovatelům služeb spojených s kryptoaktivy",
        "hint": "us_vasp",
    },
    # Insurance / IDD
    {"label": "Úřední sdělení ČNB k IDD – distribuce pojištění", "hint": "us_idd"},
    {
        "label": "Úřední sdělení ČNB k POG (product oversight & governance, IDD)",
        "hint": "us_pog_idd",
    },
    # Cross-cutting / supervisory
    {"label": "Úřední sdělení ČNB k metodice řízení rizik", "hint": "us_rizika"},
    {"label": "Úřední sdělení ČNB k oznamování protiprávního jednání", "hint": "us_oznameni"},
    {"label": "Úřední sdělení ČNB k dohledové komunikaci", "hint": "us_dohledova_komunikace"},
    {"label": "Úřední sdělení ČNB k registračnímu řízení", "hint": "us_registracni_rizeni"},
    {"label": "Úřední sdělení ČNB k mimořádným opatřením a sankcím", "hint": "us_sankce"},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB, generous for ČNB scans


def _cnb_dir() -> Path:
    target = get_settings().data_dir / "cnb_pdfs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _safe_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    base = _SAFE_NAME_RE.sub("_", base)
    if not base.lower().endswith(".pdf"):
        base = base + ".pdf"
    return base[:120]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@html_router.get("", include_in_schema=False)
async def _admin_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/", status_code=307)


@html_router.get("/", response_class=HTMLResponse)
async def admin_dashboard() -> str:
    return _DASHBOARD_HTML


@router.get("/api/stats")
async def admin_stats() -> dict[str, Any]:
    """Return corpus counts and progress against the ingestion targets, for the dashboard cards."""
    async with session_scope() as session:
        pub_rows = (
            await session.execute(
                select(SourceDocument.publisher, func.count(SourceDocument.id)).group_by(
                    SourceDocument.publisher
                )
            )
        ).all()
        publishers = [{"publisher": p, "n_documents": n} for p, n in pub_rows]

        n_provisions = (await session.execute(select(func.count(Provision.id)))).scalar_one()
        n_xrefs = (await session.execute(text("SELECT count(*) FROM cross_reference"))).scalar_one()
        n_sentences = (await session.execute(select(func.count(ProvisionSentence.id)))).scalar_one()
        n_definitions = (await session.execute(select(func.count(DefinedTerm.id)))).scalar_one()

        recent = (
            await session.execute(
                select(
                    SourceDocument.publisher,
                    SourceDocument.external_id,
                    SourceDocument.title,
                    SourceDocument.fetched_at,
                    func.count(Provision.id).label("n_prov"),
                )
                .outerjoin(Provision, Provision.source_document_id == SourceDocument.id)
                .group_by(SourceDocument.id)
                .order_by(SourceDocument.fetched_at.desc())
                .limit(15)
            )
        ).all()
        recent_list = [
            {
                "publisher": r.publisher,
                "external_id": r.external_id,
                "title": (r.title or "")[:90],
                "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
                "n_provisions": r.n_prov,
            }
            for r in recent
        ]

    def _count(pub: str) -> int:
        return next((p["n_documents"] for p in publishers if p["publisher"] == pub), 0)

    pdfs_on_disk = len(discover_pdfs(_cnb_dir()))

    return {
        "publishers": publishers,
        "totals": {
            "documents": sum(p["n_documents"] for p in publishers),
            "provisions": n_provisions,
            "cross_references": n_xrefs,
            "sentences": n_sentences,
            "definitions": n_definitions,
            "cnb_pdfs_on_disk": pdfs_on_disk,
        },
        "acceptance": {
            "eurlex": {"target": 6, "actual": _count("EUR-Lex")},
            "sbirka": {"target": 5, "actual": _count("Sbírka zákonů")},
            "cnb": {"target": 25, "actual": _count("ČNB")},
            "provisions": {"target_low": 4000, "target_high": 7000, "actual": n_provisions},
            "sentences": {"target_low": 16000, "target_high": 35000, "actual": n_sentences},
            "definitions": {"target_low": 200, "target_high": 1500, "actual": n_definitions},
        },
        "recent_documents": recent_list,
    }


@router.get("/api/logs")
async def admin_logs(
    level: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Return the tail of logs/app.log.

    Optional filters: `level` (info, warning or error) and a substring `q`.
    """
    log_path = get_settings().log_dir / "app.log"
    if not log_path.exists():
        return {"entries": [], "log_path": str(log_path), "note": "log file not yet created"}

    try:
        with log_path.open("r", encoding="utf-8") as fh:
            tail = fh.readlines()[-4000:]
    except OSError as exc:
        return {"entries": [], "error": str(exc), "log_path": str(log_path)}

    entries: list[dict[str, Any]] = []
    needle = q.lower() if q else None
    level_norm = level.lower() if level else None
    for raw in tail:
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        row_level = (row.get("level") or "info").lower()
        if level_norm and row_level != level_norm:
            continue
        if needle and needle not in raw.lower():
            continue
        entries.append(
            {
                "timestamp": row.get("timestamp", ""),
                "level": row_level,
                "code": row.get("code") or row.get("event", ""),
                "friendly_message": row.get("friendly_message", ""),
                "diagnostic_message": row.get("diagnostic_message", ""),
                "project_id": row.get("project_id"),
                "payload": row.get("payload"),
            }
        )

    return {
        "entries": entries[-limit:],
        "total_scanned": len(tail),
        "log_path": str(log_path),
    }


@router.get("/cnb/", response_model=None)
async def list_cnb_pdfs() -> dict[str, Any]:
    """List the ČNB PDFs on disk with their ingestion state in the database."""
    pdfs = discover_pdfs(_cnb_dir())
    pdf_stems = {p.stem for p in pdfs}

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    SourceDocument.external_id,
                    SourceDocument.id,
                    SourceDocument.title,
                    SourceDocument.metadata_,
                    SourceDocument.fetched_at,
                ).where(SourceDocument.publisher == "ČNB")
            )
        ).all()
        ingested = {r.external_id: r for r in rows}
        # Provision counts per document
        ids = [r.id for r in rows]
        prov_counts: dict[str, int] = {}
        if ids:
            count_rows = (
                await session.execute(
                    select(SourceDocument.external_id, Provision.id)
                    .join(Provision, Provision.source_document_id == SourceDocument.id)
                    .where(SourceDocument.id.in_(ids))
                )
            ).all()
            for ext_id, _ in count_rows:
                prov_counts[ext_id] = prov_counts.get(ext_id, 0) + 1

    files = []
    for p in pdfs:
        row = ingested.get(p.stem)
        files.append(
            {
                "filename": p.name,
                "stem": p.stem,
                "size_bytes": p.stat().st_size,
                "ingested": row is not None,
                "n_provisions": prov_counts.get(p.stem, 0) if row else 0,
                "fetched_at": row.fetched_at.isoformat() if row else None,
                "split_mode": (row.metadata_ or {}).get("split_mode") if row else None,
            }
        )

    return {
        "folder": str(_cnb_dir()),
        "total_files": len(pdfs),
        "total_ingested": sum(1 for f in files if f["ingested"]),
        "files": files,
        "uningested_stems": sorted(pdf_stems - set(ingested.keys())),
        "suggested": SUGGESTED_CNB,
    }


@router.post("/cnb/upload")
async def upload_cnb_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
    """Upload a ČNB PDF, store it under data/cnb_pdfs/ and ingest it synchronously.

    Returns 400 for an empty or non-PDF upload, 413 above 50 MB, 503 when free disk space would
    drop below the configured floor, and 500 if ingestion fails.
    """
    if not file.filename:
        raise HTTPException(400, "filename missing")
    if (file.content_type or "").lower() not in {"application/pdf", "application/octet-stream", ""}:
        raise HTTPException(400, f"unexpected content_type {file.content_type!r}")

    safe = _safe_filename(file.filename)
    target = _cnb_dir() / safe
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty upload")
    if len(data) > _MAX_PDF_BYTES:
        raise HTTPException(413, f"upload too large ({len(data)} bytes)")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "not a PDF (missing %PDF- magic header)")

    # Storage gate: refuse before writing the upload if it would push free disk space below the
    # floor, returning a clean 503 instead of crashing on a full disk. ingest_pdf runs the same gate
    # for the CLI path, but here it must run before target.write_bytes.
    try:
        await resource_guard.gate_storage(required_bytes=len(data))
    except resource_guard.ResourceLimitExceeded as exc:
        raise HTTPException(503, exc.reason) from exc

    target.write_bytes(data)

    try:
        result = await ingest_pdf(target)
    except resource_guard.ResourceLimitExceeded as exc:
        # The gate inside ingestion refused (for example, free disk dropped below the floor between
        # the upload write and the raw-artefact write). Report 503, not a generic 500.
        raise HTTPException(503, exc.reason) from exc
    except Exception as exc:
        raise HTTPException(500, f"ingestion failed: {exc!r}") from exc

    return {
        "filename": safe,
        "stored_at": str(target),
        "size_bytes": len(data),
        **result,
    }


# ---------------------------------------------------------------------------
# Platform observability: live system metrics, spend, kill switch, limits.
# (`require_control_token` is applied to the whole router; see the top of this module.)
# ---------------------------------------------------------------------------
@router.get("/api/system")
async def admin_system(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return the snapshot behind the System panel's resource and safety cards.

    `resources` is null until the collector's first sample, and the panel shows "warming up"
    rather than a fake zero.
    """
    # Read the single-producer collector's ring buffer. Calling system_resources() here would
    # reset the collector's cpu_percent baseline and corrupt its readings.
    sample = system_monitor.latest()
    spend = await limits.spend_summary()
    cfg = limits.get_limits()
    storage_used = metrics._dir_total(get_settings().data_dir)
    storage_limit = (cfg.get("storage_gb") or 0) * (1024**3)
    over_storage = bool(storage_limit and storage_used > storage_limit)
    return {
        "resources": sample,
        "collector": system_monitor.collector_meta(),
        "memory_segmentation": system_monitor.latest_processes(),
        "mps": (sample or {}).get("mps"),
        "pressure": (sample or {}).get("pressure"),
        "spend": spend,
        "limits": cfg,
        "storage": {
            "data_dir_bytes": storage_used,
            "limit_bytes": storage_limit,
            "over_limit": over_storage,
            "percent": round(100 * storage_used / storage_limit, 2) if storage_limit else 0.0,
        },
        "kill_switch": killswitch.state(),
    }


@router.get("/api/system/history")
async def admin_system_history(
    window: int = 3600,
    max_points: int = 500,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Return resource trend series for the System panel.

    Windows up to one hour come from the in-memory ring buffer; longer windows come from the
    down-sampled system_sample table, bucketed to at most `max_points` points.
    """
    return await system_monitor.history(window, max_points=max_points)


async def _system_event_stream() -> AsyncGenerator[dict[str, Any], None]:
    """Replay recent events on the control-room __system__ channel, then tail live ones.

    The replay (replay_recent(SYSTEM_CHANNEL)) reads the durable log, so history survives an app
    restart. Releases the subscriber slot on disconnect.
    """
    try:
        recent = await replay_recent(SYSTEM_CHANNEL, limit=100)
    except Exception:
        recent = []
    for past in recent:
        yield {"data": json.dumps(past)}
    queue: asyncio.Queue[Event] = subscribe(SYSTEM_CHANNEL, maxsize=1000)
    try:
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=15)
                yield {"data": json.dumps(evt)}
            except TimeoutError:
                yield {"event": "ping", "data": ""}
    finally:
        unsubscribe(SYSTEM_CHANNEL, queue)


@router.get("/api/events")
async def admin_events(
    _: None = Depends(require_control_token),
) -> EventSourceResponse:
    """Stream every control-room event (system, Nexus, Pythia, app) as server-sent events.

    Recent events are replayed first. The operator's control panel opens exactly one of these
    streams.
    """
    return EventSourceResponse(_system_event_stream())


@router.get("/api/storage")
async def admin_storage(top_n: int = 60, max_depth: int = 3) -> dict[str, Any]:
    """Return a per-directory size breakdown of data/ and logs/ (computed on every call)."""
    return metrics.storage_breakdown(max_depth=max_depth, top_n=top_n)


@router.get("/api/storage-segmentation")
async def admin_storage_segmentation() -> dict[str, Any]:
    """Return whole-project disk usage by category, plus the Postgres database size.

    Categories: venv, node_modules, code, data, database dump, logs, git and dev docs. The database
    part includes its largest tables. The filesystem walk is cached for about 45 seconds.
    """
    seg = metrics.project_storage_segmentation()
    buckets = list(seg["buckets"])
    db_tables: list[dict[str, Any]] = []
    db_total = -1
    try:
        async with session_scope() as session:
            db_total = int(
                (
                    await session.execute(text("SELECT pg_database_size(current_database())"))
                ).scalar_one()
            )
            rows = (
                await session.execute(
                    text(
                        """
                SELECT c.relname, pg_total_relation_size(c.oid) AS bytes
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.relkind = 'r' AND n.nspname = 'public'
                 ORDER BY bytes DESC LIMIT 12
                """
                    )
                )
            ).all()
            db_tables = [{"table": r[0], "size_bytes": int(r[1])} for r in rows]
    except Exception as exc:
        db_tables = [{"table": f"(db query failed: {exc!r})", "size_bytes": 0}]
    if db_total >= 0:
        buckets.append(
            {
                "key": "database",
                "label": "Database (Postgres, logical)",
                "size_bytes": db_total,
                "n_files": len(db_tables),
                "note": "pg_database_size — stored in Docker volume polarius_regsuper_pgdata",
            }
        )
    buckets.sort(key=lambda b: b["size_bytes"], reverse=True)
    grand = sum(b["size_bytes"] for b in buckets)
    for b in buckets:
        b["percent"] = round(100 * b["size_bytes"] / grand, 1) if grand else 0.0
    return {
        "root": seg["root"],
        "buckets": buckets,
        "db_tables": db_tables,
        "grand_total_bytes": grand,
    }


@router.get("/api/cost-audit")
async def admin_cost_audit() -> dict[str, Any]:
    """Explain the spend figure: pricing source, unpriced calls and a per-model breakdown.

    Successful calls that could not be priced (unknown model) mean the total is an undercount;
    their count is reported. The breakdown lists calls, tokens and cost per model.
    """
    async with session_scope() as session:
        total_calls = int(
            (await session.execute(text("SELECT count(*) FROM api_call"))).scalar_one()
        )
        uncosted = int(
            (
                await session.execute(
                    text(
                        """
            SELECT count(*) FROM api_call
             WHERE status = 'ok' AND coalesce(cost_usd, 0) = 0
               AND coalesce(tokens_input, 0) > 0
               AND coalesce(provider, '') <> 'local'
            """
                    )
                )
            ).scalar_one()
        )
        rows = (
            await session.execute(
                text(
                    """
            SELECT provider, model, count(*),
                   coalesce(sum(cost_usd), 0),
                   coalesce(sum(tokens_input), 0),
                   coalesce(sum(tokens_output), 0)
              FROM api_call
             GROUP BY provider, model
             ORDER BY 4 DESC
            """
                )
            )
        ).all()
    return {
        "pricing_as_of": "OpenAI/Cohere list 2025-Q1 — verify against current price sheet",
        "known_models": sorted(api_tracker._PRICES.keys()),
        "uncosted_calls": uncosted,
        "total_calls": total_calls,
        "by_model": [
            {
                "provider": r[0],
                "model": r[1],
                "n": int(r[2]),
                "cost_usd": float(r[3]),
                "tok_in": int(r[4]),
                "tok_out": int(r[5]),
            }
            for r in rows
        ],
    }


@router.post("/api/killswitch/engage")
async def admin_killswitch_engage(
    payload: dict[str, Any] = Body(default={}),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    reason = str(payload.get("reason") or "manual").strip()[:500] or "manual"
    st = killswitch.engage(reason)
    # killswitch.engage is synchronous and emits nothing by design; the endpoint emits so the trip
    # reaches the __system__ event channel and the alarm bar.
    await emit("killswitch.engaged", module="polarius", level="error", reason=reason)
    return st


@router.post("/api/killswitch/release")
async def admin_killswitch_release(_: None = Depends(require_control_token)) -> dict[str, Any]:
    st = killswitch.release()
    await emit("killswitch.released", module="polarius")
    return st


@router.get("/api/limits")
async def admin_limits_get(_: None = Depends(require_control_token)) -> dict[str, Any]:
    return limits.get_limits()


@router.post("/api/limits")
async def admin_limits_set(
    patch: dict[str, Any] = Body(...),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    after = limits.set_limits(patch)
    # Audit trail: a lowered cap that later causes refusals must leave a trace.
    await emit("limits.cap_changed", module="polarius", changed=sorted(patch.keys()))
    return after


# ── Control endpoints: weekly per-key budgets, keys, resource ceilings, watchdog, alarms ──────────
@router.get("/api/weekly")
async def admin_weekly(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return weekly spend against the cap for each API key (the budget grid).

    Configured keys are registered first, so an unused key still shows a budget line.
    """
    keys.ensure_registered()
    return await limits.weekly_summary()


@router.get("/api/keys")
async def admin_keys_get(_: None = Depends(require_control_token)) -> dict[str, Any]:
    return {"keys": keys.ensure_registered()}


@router.post("/api/keys")
async def admin_keys_set(
    patch: dict[str, Any] = Body(...),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    kid = str(patch.get("key_id") or "")
    if not kid:
        raise HTTPException(422, "key_id required")
    return {"keys": keys.set_label(kid, str(patch.get("label") or ""))}


@router.get("/api/resources")
async def admin_resources(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return current usage against each hard resource cap, and the action each cap triggers.

    Feeds the ResourceCeilings panel. Reads the collector's ring buffer and never calls psutil
    directly, which would disturb the collector's CPU baseline.
    """
    caps = limits.get_limits().get("resource_caps") or {}
    sample = system_monitor.latest() or {}
    mem = sample.get("memory") or {}
    disk = sample.get("disk") or {}
    cpu = sample.get("cpu") or {}
    mps = metrics.mps_memory()
    free_gb = float(disk.get("free_bytes") or 0) / (1024**3)
    return {
        "watchdog_enabled": bool(caps.get("watchdog_enabled", True)),
        "ceilings": [
            {
                "key": "memory",
                "label": "Memory",
                "current_pct": mem.get("percent"),
                "ceiling_pct": caps.get("mem_refuse_pct"),
                "unit": "%",
                "action": "refuse new analysis (429); watchdog → kill-switch",
            },
            {
                "key": "cpu",
                "label": "CPU",
                "current_pct": cpu.get("percent"),
                "ceiling_pct": caps.get("cpu_refuse_pct"),
                "unit": "%",
                "action": "refuse new analysis (429)",
            },
            {
                "key": "storage",
                "label": "Free disk",
                "current_gb": round(free_gb, 1),
                "floor_gb": caps.get("min_free_disk_gb"),
                "unit": "GB free",
                "action": "refuse heavy writes (507); pre-op gate + mid-run advisory",
            },
            {
                "key": "mps",
                "label": "GPU / MPS",
                "available": mps.get("available"),
                "current_gb": (
                    round(float(mps.get("allocated_bytes") or 0) / (1024**3), 2)
                    if mps.get("available")
                    else None
                ),
                "ceiling_gb": caps.get("max_mps_mem_gb"),
                "unit": "GB",
                "note": mps.get("reason"),
                "action": "refuse a new GNN run (only while a GNN job is live)",
            },
        ],
        "kill_switch": killswitch.state(),
    }


@router.get("/api/resource-limits")
async def admin_reslimits_get(_: None = Depends(require_control_token)) -> dict[str, Any]:
    return limits.get_limits().get("resource_caps") or {}


@router.post("/api/resource-limits")
async def admin_reslimits_set(
    patch: dict[str, Any] = Body(...),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    after = limits.set_limits({"resource_caps": patch})
    await emit(
        "limits.cap_changed",
        module="polarius",
        changed=["resource_caps"],
        detail=sorted(patch.keys()),
    )
    return after.get("resource_caps") or {}


@router.post("/api/watchdog/toggle")
async def admin_watchdog_toggle(
    payload: dict[str, Any] = Body(default={}),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    enabled = bool(payload.get("enabled", True))
    after = limits.set_limits({"resource_caps": {"watchdog_enabled": enabled}})
    await emit(
        "limits.cap_changed",
        module="polarius",
        changed=["resource_caps"],
        detail=["watchdog_enabled"],
    )
    return {"watchdog_enabled": (after.get("resource_caps") or {}).get("watchdog_enabled", True)}


@router.get("/api/alarms")
async def admin_alarms(limit: int = 50, _: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return recent warning and error control events.

    Read from the durable structlog file, so they survive a page reload.
    """
    recent = await replay_recent(SYSTEM_CHANNEL, limit=400)
    alarms = [e for e in recent if e.get("level") in ("warn", "warning", "error")]
    return {"alarms": alarms[-limit:]}


# ── Polarius (app analysis module) panel feeds ───────────────────────────────────────────────────
@router.get("/api/polarius/north-star")
async def admin_polarius_northstar(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return the north-star metric (cascade_grounded_yield) and the citation-verdict breakdown."""
    return await call_stats.citation_health()


@router.get("/api/polarius/calls")
async def admin_polarius_calls(
    window: int = 86_400,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Count the external calls the app initiates, per provider and model.

    Includes calls to the Pythia engine; those must not be added to the engine's own internal
    spend, which would count them twice.
    """
    return await call_stats.call_counts(window)


@router.get("/api/polarius/cascades")
async def admin_polarius_cascades(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return recent analysis-cascade runs (the CascadeBoard feed) and the number of active runs."""
    return {"active": cascade_tracker.active_count(), "runs": cascade_tracker.recent(30)}


# ── Pythia panel feeds ───────────────────────────────────────────────────────────────────────────
# Proxies of the engine's own telemetry. They always answer 200 with `enabled` and `reachable`
# markers, so the panel can show an off or unreachable state instead of an error.
def _pythia_on() -> bool:
    s = get_settings()
    return bool(s.pythia_enabled and s.pythia_url)


@router.get("/api/pythia/metrics")
async def admin_pythia_metrics(_: None = Depends(require_control_token)) -> dict[str, Any]:
    if not _pythia_on():
        return {"enabled": False, "reason": "PYTHIA_ENABLED is off"}
    try:
        return {"enabled": True, "reachable": True, **(await pythia_client.metrics())}
    except Exception as exc:
        return {"enabled": True, "reachable": False, "error": type(exc).__name__}


@router.get("/api/pythia/telemetry")
async def admin_pythia_telemetry(
    limit: int = 50,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    if not _pythia_on():
        return {"enabled": False, "traces": []}
    try:
        return {"enabled": True, "reachable": True, "traces": await pythia_client.telemetry(limit)}
    except Exception as exc:
        return {"enabled": True, "reachable": False, "traces": [], "error": type(exc).__name__}


@router.get("/api/pythia/panel-health")
async def admin_pythia_panel_health(_: None = Depends(require_control_token)) -> dict[str, Any]:
    if not _pythia_on():
        return {"enabled": False}
    try:
        return {"enabled": True, "reachable": True, **(await pythia_client.health())}
    except Exception as exc:
        return {"enabled": True, "reachable": False, "error": type(exc).__name__}


# ── Nexus panel feeds ────────────────────────────────────────────────────────────────────────────
# Nexus statistics and events, reached through the Pythia engine, with the same off and unreachable
# markers as the Pythia feeds.
@router.get("/api/nexus/stats")
async def admin_nexus_stats(_: None = Depends(require_control_token)) -> dict[str, Any]:
    if not _pythia_on():
        return {
            "enabled": False,
            "reason": "Nexus is reached via the Pythia sidecar (PYTHIA_ENABLED off)",
        }
    try:
        return {"enabled": True, "reachable": True, **(await pythia_client.nexus_stats())}
    except Exception as exc:
        return {"enabled": True, "reachable": False, "error": type(exc).__name__}


@router.get("/api/nexus/events")
async def admin_nexus_events(
    limit: int = 50,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    if not _pythia_on():
        return {"enabled": False, "events": []}
    try:
        return {
            "enabled": True,
            "reachable": True,
            "events": await pythia_client.nexus_events(limit),
        }
    except Exception as exc:
        return {"enabled": True, "reachable": False, "events": [], "error": type(exc).__name__}


# ── Auto-ingestion controls ──────────────────────────────────────────────────────────────────────
# Status, run-once and history proxy the engine (untracked reads plus one write) with the same
# {enabled, reachable, ...} envelope as admin_nexus_stats. Enable and disable are owned by the app:
# they write the persisted flag in limits.json and mirror it to data/AUTO_INGEST_ENABLED for the
# engine, which never owns the on/off state.
@router.get("/api/ingestion/status")
async def admin_ingestion_status(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Return the engine's ingestion status merged with the app's master switch.

    Status covers the latest run, `next_run_at`, `queue_pending` and corpus metrics. `enabled`
    always reflects the app's switch, even when the engine is off or unreachable, so the panel
    shows the true on/off state regardless of the engine's reachability.
    """
    enabled = limits.get_auto_ingest_enabled()
    if not _pythia_on():
        return {"enabled": enabled, "reachable": False, "reason": "PYTHIA_ENABLED is off"}
    try:
        status = await pythia_client.ingestion_status()
        # The app's flag is authoritative for `enabled`; it overrides any value the engine reports.
        return {**status, "enabled": enabled, "reachable": True}
    except Exception as exc:
        return {"enabled": enabled, "reachable": False, "error": type(exc).__name__}


@router.post("/api/ingestion/enable")
async def admin_ingestion_enable(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Turn auto-ingestion on.

    Persists the flag in limits.json (the source of truth) and writes the mirror file the engine
    reads, so it survives restarts. Does not start a run itself: the engine picks up the flag at
    its next cycle or on a manual run-once.
    """
    enabled = limits.set_auto_ingest_enabled(True)
    await emit("auto-ingest.toggle", module="polarius", enabled=enabled)
    return {"enabled": enabled}


@router.post("/api/ingestion/disable")
async def admin_ingestion_disable(_: None = Depends(require_control_token)) -> dict[str, Any]:
    """Turn auto-ingestion off.

    Persists the flag and its mirror file. If a run is in flight, the kill switch is also engaged
    so the cycle stops cleanly (the orchestrator checks it between stages). If the engine is
    unreachable the flag is still turned off, so the next cycle reads it; the kill switch is
    engaged only when a running cycle was actually observed.
    """
    enabled = limits.set_auto_ingest_enabled(False)
    running = False
    if _pythia_on():
        try:
            status = await pythia_client.ingestion_status()
            running = bool(status.get("running"))
        except Exception:
            running = False  # engine off or unreachable, so nothing to stop
    if running:
        killswitch.engage("auto-ingestion disabled mid-run")
        await emit(
            "auto-ingest.halt.killswitch",
            module="polarius",
            level="warn",
            reason="disabled mid-run",
        )
    await emit("auto-ingest.toggle", module="polarius", enabled=enabled, killswitch=running)
    return {"enabled": enabled, "killswitch_engaged": running}


@router.post("/api/ingestion/run-once")
async def admin_ingestion_run_once(
    payload: dict[str, Any] = Body(default={}),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Start one ingestion run on the engine (only one can run at a time).

    An optional `dry_run` in the body is forwarded; when omitted the engine's default applies.
    Returns 409 when a run is already in flight, so the UI can say so; other engine errors map
    to 502.
    """
    if not _pythia_on():
        return {
            "enabled": limits.get_auto_ingest_enabled(),
            "reachable": False,
            "started": False,
            "reason": "PYTHIA_ENABLED is off",
        }
    dry_run = payload.get("dry_run")
    if dry_run is not None:
        dry_run = bool(dry_run)
    try:
        result = await pythia_client.ingestion_run_once(dry_run)
        return {"enabled": limits.get_auto_ingest_enabled(), "reachable": True, **result}
    except httpx.HTTPStatusError as exc:
        # Pass the engine's 409 (already_running) straight through; map other statuses to 502.
        if exc.response is not None and exc.response.status_code == 409:
            raise HTTPException(status_code=409, detail="already_running") from exc
        raise HTTPException(status_code=502, detail="sidecar run-once failed") from exc
    except Exception as exc:
        return {
            "enabled": limits.get_auto_ingest_enabled(),
            "reachable": False,
            "started": False,
            "error": type(exc).__name__,
        }


@router.post("/api/ingestion/stop")
async def admin_ingestion_stop(
    payload: dict[str, Any] = Body(default={}),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Stop the in-flight ingestion cycle by engaging the kill switch.

    The cycle stops cleanly at its next check between stages or acts; each amendment is its own
    transaction, and a deferred reindex simply does not run. The master switch is left as it is,
    so auto-ingestion stays enabled for the next cycle unless the operator also disables it.
    """
    reason = (
        str(payload.get("reason") or "auto-ingestion stop").strip()[:500] or "auto-ingestion stop"
    )
    st = killswitch.engage(reason)
    await emit("killswitch.engaged", module="polarius", level="error", reason=reason)
    return st


@router.get("/api/ingestion/history")
async def admin_ingestion_history(
    limit: int = 50,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Return recent ingestion runs for the history table, with the off and unreachable markers."""
    if not _pythia_on():
        return {"enabled": limits.get_auto_ingest_enabled(), "reachable": False, "runs": []}
    try:
        hist = await pythia_client.ingestion_history(limit)
        runs = hist.get("runs", []) if isinstance(hist, dict) else hist
        return {"enabled": limits.get_auto_ingest_enabled(), "reachable": True, "runs": runs}
    except Exception as exc:
        return {
            "enabled": limits.get_auto_ingest_enabled(),
            "reachable": False,
            "runs": [],
            "error": type(exc).__name__,
        }


@router.get("/api/schema")
async def admin_schema(format: str = "json", source: str = "live") -> Any:
    """Return the database schema, reflected from the running PostgreSQL by default.

    `source=metadata` reads the SQLAlchemy ORM declarations instead: faster and without a database
    query, but it misses indexes and generated columns that exist only in migrations.
    """
    if source == "metadata":
        if format == "markdown":
            return {"markdown": db_schema.render_markdown()}
        return db_schema.render_json()
    try:
        if format == "markdown":
            return {"markdown": await db_schema.render_markdown_live()}
        return await db_schema.reflect_live_schema()
    except Exception:
        # Database unreachable: fall back to ORM metadata so the page still works.
        if format == "markdown":
            return {"markdown": db_schema.render_markdown()}
        return db_schema.render_json()


@html_router.get("/schema", response_class=HTMLResponse)
async def admin_schema_page() -> str:
    return _SCHEMA_HTML


# ---------------------------------------------------------------------------
# Map of EU directives to the Czech acts that transpose them (read-only).
#
# The curated map lives in `src/regsuper/analysis/transposition_map.py`. The endpoint joins it with
# live `source_document` counts so the operator can check coverage: which directives are loaded,
# and whether both sides have provisions.
# ---------------------------------------------------------------------------
@router.get("/api/transpositions")
async def admin_transpositions() -> dict[str, Any]:
    """List EU directives with their Czech transposing acts, and directly applicable EU regulations.

    Each entry carries live provision counts. The analysis cascade does not use this map yet.
    """
    return await transposition_map.coverage_report()


# ---------------------------------------------------------------------------
# API call inspector: list calls, view details, replay with edits.
# ---------------------------------------------------------------------------
@router.get("/api/calls")
async def admin_calls_list(
    limit: int = 100,
    source: str | None = None,
    status: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    async with session_scope() as session:
        stmt = (
            select(
                ApiCall.id,
                ApiCall.ts,
                ApiCall.provider,
                ApiCall.model,
                ApiCall.endpoint,
                ApiCall.source,
                ApiCall.tokens_input,
                ApiCall.tokens_output,
                ApiCall.tokens_cached,
                ApiCall.cost_usd,
                ApiCall.latency_ms,
                ApiCall.status,
                ApiCall.response_summary,
                ApiCall.error,
            )
            .order_by(desc(ApiCall.ts))
            .limit(min(max(limit, 1), 500))
        )
        if source:
            stmt = stmt.where(ApiCall.source == source)
        if status:
            stmt = stmt.where(ApiCall.status == status)
        if provider:
            stmt = stmt.where(ApiCall.provider == provider)
        rows = (await session.execute(stmt)).all()
        # Distinct sources for the filter dropdown.
        sources = (
            (await session.execute(select(ApiCall.source).distinct().order_by(ApiCall.source)))
            .scalars()
            .all()
        )

    out = [
        {
            "id": str(r.id),
            "ts": r.ts.isoformat() if r.ts else None,
            "provider": r.provider,
            "model": r.model,
            "endpoint": r.endpoint,
            "source": r.source,
            "tokens_input": r.tokens_input,
            "tokens_output": r.tokens_output,
            "tokens_cached": r.tokens_cached,
            "cost_usd": float(r.cost_usd or 0),
            "latency_ms": r.latency_ms,
            "status": r.status,
            "summary": (r.response_summary or "")[:200],
            "error": (r.error or "")[:200] if r.error else None,
        }
        for r in rows
    ]
    return {"calls": out, "sources": sources}


@router.get("/api/calls/{call_id}")
async def admin_call_detail(call_id: UUID) -> dict[str, Any]:
    async with session_scope() as session:
        row = (
            await session.execute(select(ApiCall).where(ApiCall.id == call_id))
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"api_call {call_id} not found")
    return {
        "id": str(row.id),
        "ts": row.ts.isoformat() if row.ts else None,
        "provider": row.provider,
        "model": row.model,
        "endpoint": row.endpoint,
        "source": row.source,
        "tokens_input": row.tokens_input,
        "tokens_output": row.tokens_output,
        "tokens_cached": row.tokens_cached,
        "cost_usd": float(row.cost_usd or 0),
        "latency_ms": row.latency_ms,
        "status": row.status,
        "error": row.error,
        "request_json": row.request_json,
        "response_json": row.response_json,
        "response_summary": row.response_summary,
    }


# Replay is the only /admin/api route that spends money (it re-issues a live OpenAI call). It is off
# by default (settings.replay_enabled) and rate-limited, so a stuck UI or retry loop cannot fan out
# paid calls.
_REPLAY_MIN_INTERVAL_S = 60.0
_replay_last_call_ts: float = 0.0


@router.post("/api/calls/{call_id}/replay")
async def admin_call_replay(
    call_id: UUID,
    overrides: dict[str, Any] = Body(default={}),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Re-run a previous OpenAI request, optionally overriding some of its parameters.

    `model`, `temperature`, `messages` and `max_tokens` can be overridden. The replay is logged
    as its own `api_call` row whose source ends in `:replay`. Because this is the only admin
    endpoint that spends money, it fails closed: it returns 403 unless ``settings.replay_enabled``
    is True (the OpenAI client is never reached), and a rate limit allows at most one replay every
    60 seconds (429 with `Retry-After` otherwise).
    """
    settings = get_settings()
    if not settings.replay_enabled:
        raise HTTPException(
            403, "replay is disabled (set REPLAY_ENABLED=true to allow live re-issue)"
        )

    # Rate limit: at most one replay per _REPLAY_MIN_INTERVAL_S, process-wide. Checked before any
    # database read or OpenAI call, so a retry loop is cheap to refuse.
    global _replay_last_call_ts
    now = time.monotonic()
    elapsed = now - _replay_last_call_ts
    if elapsed < _REPLAY_MIN_INTERVAL_S:
        retry_after = int(_REPLAY_MIN_INTERVAL_S - elapsed) + 1
        raise HTTPException(
            429,
            f"replay rate-limited; retry in {retry_after}s",
            headers={"Retry-After": str(retry_after)},
        )
    _replay_last_call_ts = now

    async with session_scope() as session:
        row = (
            await session.execute(select(ApiCall).where(ApiCall.id == call_id))
        ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, f"api_call {call_id} not found")
    if row.provider != "openai":
        raise HTTPException(400, f"replay only supports OpenAI calls (got {row.provider})")
    if row.endpoint not in {"chat.completions", "embeddings"}:
        raise HTTPException(400, f"unsupported endpoint {row.endpoint}")

    if not settings.openai_api_key:
        raise HTTPException(500, "OPENAI_API_KEY not configured")

    # Build kwargs from the stored request plus the caller's overrides.
    kwargs = dict(row.request_json or {})
    for k, v in (overrides or {}).items():
        if v is None:
            kwargs.pop(k, None)
        else:
            kwargs[k] = v
    if "model" not in kwargs:
        kwargs["model"] = row.model

    if row.endpoint == "embeddings" and isinstance(kwargs.get("input"), dict):
        # _request_snapshot strips embedding inputs, so the caller must supply them again.
        raise HTTPException(
            400,
            "embedding inputs are not retained; pass `input` in overrides to replay",
        )

    client = get_openai()
    source = f"{row.source}:replay"
    try:
        if row.endpoint == "chat.completions":
            resp = await api_tracker.tracked_chat_completion(client, source=source, **kwargs)
            content = resp.choices[0].message.content if resp.choices else ""
            return {"ok": True, "summary": (content or "")[:1000]}
        else:
            resp = await api_tracker.tracked_embedding(client, source=source, **kwargs)
            n = len(resp.data)
            return {"ok": True, "summary": f"{n} embeddings"}
    except Exception as exc:
        raise HTTPException(500, f"replay failed: {exc!r}") from exc


@html_router.get("/calls", response_class=HTMLResponse)
async def admin_calls_page() -> str:
    return _CALLS_HTML


@router.post("/cnb/reingest")
async def reingest_all() -> dict[str, Any]:
    """Re-ingest every ČNB PDF on disk. Idempotent: each document is deleted and re-inserted."""
    folder = _cnb_dir()
    pdfs = discover_pdfs(folder)
    results = []
    for pdf in pdfs:
        try:
            results.append(await ingest_pdf(pdf))
        except Exception as exc:
            results.append({"external_id": pdf.stem, "error": repr(exc)})
    return {"folder": str(folder), "total": len(pdfs), "results": results}


@html_router.get("/cnb", response_class=HTMLResponse)
async def cnb_upload_page() -> str:
    """Serve the single-page ČNB uploader (plain HTML and JavaScript, no build step).

    Files are dropped or picked and posted to `/admin/cnb/upload`. The suggestions checklist is
    rendered server-side so the lawyer can tick items off.
    """
    # The suggestions are rendered into the HTML body; the file list and upload actions call the
    # JSON endpoints above via `fetch`.
    suggestions_html = "\n".join(
        f'<li><label><input type="checkbox" data-hint="{s["hint"]}"> {s["label"]}</label></li>'
        for s in SUGGESTED_CNB
    )
    return _HTML_TEMPLATE.replace("{{SUGGESTIONS}}", suggestions_html)


_HTML_TEMPLATE = """<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8" />
  <title>ČNB PDF uploader — RegSuper</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { font-family: system-ui, -apple-system, sans-serif; color: #111; }
    body { max-width: 980px; margin: 2rem auto; padding: 0 1.5rem; line-height: 1.45; }
    h1 { margin-bottom: 0.2rem; }
    h2 { margin-top: 2rem; border-bottom: 1px solid #ccc; padding-bottom: 0.3rem; }
    .grid { display: grid; grid-template-columns: 2fr 3fr; gap: 2rem; }
    @media (max-width: 800px) { .grid { grid-template-columns: 1fr; } }
    #drop {
      border: 2px dashed #5a8;
      border-radius: 10px;
      padding: 2rem;
      text-align: center;
      color: #555;
      transition: background 0.15s ease, border-color 0.15s ease;
    }
    #drop.over { background: #eef9f3; border-color: #2a6; }
    button { font: inherit; padding: 0.4rem 0.9rem; cursor: pointer; }
    table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
    th, td { border-bottom: 1px solid #eee; padding: 0.35rem 0.4rem; text-align: left; }
    .ok { color: #2a6; font-weight: 600; }
    .miss { color: #b53; font-weight: 600; }
    ul.checklist { list-style: none; padding-left: 0; font-size: 0.95em; }
    ul.checklist li { padding: 0.18rem 0; }
    pre.log { background: #111; color: #cfe; padding: 1rem; border-radius: 8px;
              max-height: 250px; overflow: auto; font-size: 0.85em; }
    small { color: #666; }
  </style>
</head>
<body>
  <h1>ČNB methodologies uploader</h1>
  <p><small>
    Drop the PDFs you want ingested into the box below. Each file is saved to
    <code>data/cnb_pdfs/</code> and parsed into provisions immediately. Ingestion
    quality &lt; 0.4 flags the document for re-processing with <code>marker</code>
    in AdvMVP. Day-4 acceptance: ≥ 25 documents.
  </small></p>

  <div class="grid">
    <section>
      <div id="drop">
        <p><strong>Drag &amp; drop PDFs here</strong></p>
        <p>— or —</p>
        <input type="file" id="picker" accept="application/pdf" multiple />
        <p><button id="reingest">Re-ingest everything on disk</button></p>
      </div>
      <h2>Suggested documents</h2>
      <ul class="checklist">
        {{SUGGESTIONS}}
      </ul>
    </section>

    <section>
      <h2>Status</h2>
      <p id="counter"><em>Loading…</em></p>
      <table id="filetable">
        <thead><tr>
          <th>File</th><th>State</th><th>Provisions</th><th>Mode</th><th>Size</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <h2>Log</h2>
      <pre class="log" id="log"></pre>
    </section>
  </div>

<script>
const drop = document.getElementById('drop');
const picker = document.getElementById('picker');
const reingest = document.getElementById('reingest');
const log = document.getElementById('log');
const tbody = document.querySelector('#filetable tbody');
const counter = document.getElementById('counter');

function logLine(msg) {
  const ts = new Date().toISOString().slice(11, 19);
  log.textContent = `[${ts}] ${msg}\\n` + log.textContent;
}

async function refresh() {
  const r = await fetch('/admin/cnb/');
  const data = await r.json();
  counter.innerHTML =
    `<strong>${data.total_ingested}</strong> ingested / ` +
    `<strong>${data.total_files}</strong> on disk. ` +
    `Folder: <code>${data.folder}</code>`;
  tbody.innerHTML = '';
  for (const f of data.files) {
    const row = document.createElement('tr');
    row.innerHTML = `
      <td>${f.filename}</td>
      <td class="${f.ingested ? 'ok' : 'miss'}">${f.ingested ? '✓ ingested' : '… pending'}</td>
      <td>${f.n_provisions || ''}</td>
      <td>${f.split_mode || ''}</td>
      <td>${(f.size_bytes/1024).toFixed(1)} KB</td>`;
    tbody.appendChild(row);
  }
}

async function uploadOne(file) {
  logLine(`uploading ${file.name} (${(file.size/1024).toFixed(1)} KB)`);
  const fd = new FormData();
  fd.append('file', file, file.name);
  const r = await fetch('/admin/cnb/upload', { method: 'POST', body: fd });
  const data = await r.json().catch(() => ({error: 'no JSON'}));
  if (!r.ok) {
    logLine(`  ✗ ${r.status} ${data.detail || data.error || JSON.stringify(data)}`);
  } else {
    logLine(`  ✓ ${data.n_provisions} provisions (mode=${data.split_mode})`);
  }
}

async function uploadFiles(files) {
  for (const f of files) {
    if (f.name.toLowerCase().endsWith('.pdf')) await uploadOne(f);
  }
  refresh();
}

drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('over'));
drop.addEventListener('drop', (e) => {
  e.preventDefault(); drop.classList.remove('over');
  uploadFiles(e.dataTransfer.files);
});
picker.addEventListener('change', (e) => uploadFiles(e.target.files));
reingest.addEventListener('click', async () => {
  reingest.disabled = true;
  logLine('re-ingesting all PDFs on disk…');
  const r = await fetch('/admin/cnb/reingest', { method: 'POST' });
  const data = await r.json();
  logLine(`re-ingest done: ${data.total} files processed`);
  refresh();
  reingest.disabled = false;
});

refresh();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Dashboard HTML: a single self-contained page with no framework and no build step.
# Polls /admin/api/stats every 5 s and /admin/api/logs every 2 s.
# ---------------------------------------------------------------------------
_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>RegSuper — admin dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { font-family: system-ui, -apple-system, sans-serif; color: #111; }
    body { max-width: 1200px; margin: 1.5rem auto; padding: 0 1.5rem; line-height: 1.45; }
    nav { display: flex; gap: 1rem; padding-bottom: 0.8rem; border-bottom: 1px solid #ddd; margin-bottom: 1rem; }
    nav a { color: #2a6; text-decoration: none; font-weight: 600; }
    nav a:hover { text-decoration: underline; }
    h1 { margin: 0.2rem 0 0.4rem; }
    h2 { margin-top: 1.6rem; border-bottom: 1px solid #eee; padding-bottom: 0.3rem; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin: 1rem 0; }
    .card { background: #f7f7f7; border: 1px solid #e3e3e3; border-radius: 8px; padding: 0.9rem 1rem; }
    .card .num { font-size: 1.8rem; font-weight: 700; color: #2a6; }
    .card .lbl { font-size: 0.85em; color: #666; text-transform: uppercase; letter-spacing: 0.04em; }
    table { border-collapse: collapse; width: 100%; font-size: 0.92em; }
    th, td { border-bottom: 1px solid #eee; padding: 0.35rem 0.5rem; text-align: left; vertical-align: top; }
    th { background: #fafafa; }
    .ok { color: #2a6; font-weight: 600; }
    .miss { color: #b53; font-weight: 600; }
    .acceptance { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1rem; }
    .accept-row { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 0.7rem 1rem; }
    .accept-row.pass { border-color: #2a6; background: #f3fbf5; }
    .accept-row.fail { border-color: #d99; background: #fdf3f0; }
    .bar { height: 8px; background: #eee; border-radius: 4px; overflow: hidden; margin: 0.4rem 0 0.2rem; }
    .bar > div { height: 100%; background: #2a6; transition: width 0.3s ease; }
    #logs { background: #111; color: #cfe; padding: 0.7rem 1rem; border-radius: 6px;
            font-family: ui-monospace, Consolas, monospace; font-size: 0.82em;
            max-height: 420px; overflow: auto; }
    #logs .row { padding: 0.15rem 0; border-bottom: 1px solid #1c1c1c; white-space: pre-wrap; }
    #logs .row.error { color: #f8a; }
    #logs .row.warning { color: #fc6; }
    #logs .ts { color: #8ad; }
    #logs .code { color: #fc6; font-weight: 600; }
    .log-controls { display: flex; gap: 0.6rem; align-items: center; margin: 0.5rem 0; }
    .log-controls input, .log-controls select { font: inherit; padding: 0.25rem 0.4rem; }
    .log-controls .spacer { flex: 1; }
    small { color: #666; }
    .err-msg { background: #fdd; color: #800; padding: 0.5rem 0.8rem; border-radius: 4px; }
    .card.kill { background: #f3fbf5; border-color: #c4e8d0; }
    .card.kill.engaged { background: #fdf3f0; border-color: #e3a8a0; }
    .card .num.engaged { color: #b53; }
    .bar > div.over { background: #b53; }
    .bar > div.warn { background: #d99; }
    #killbanner { background: #d33; color: white; padding: 0.7rem 1rem; border-radius: 6px;
                  margin: 0 0 1rem; font-weight: 600; }
    details > summary { cursor: pointer; padding: 0.4rem 0; }
    details[open] > summary { font-weight: 600; }
  </style>
</head>
<body>
  <nav>
    <a href="/admin/">Dashboard</a>
    <a href="/admin/cnb">ČNB Upload</a>
    <a href="/admin/schema">DB schema</a>
    <a href="/admin/calls">API calls</a>
    <a href="/health" target="_blank">Health JSON</a>
    <a href="/docs" target="_blank">API docs</a>
  </nav>
  <h1>RegSuper admin</h1>
  <p><small>Live system view. Refreshes every 5 s. Logs every 2 s.</small></p>

  <div id="killbanner" style="display:none"></div>

  <h2>Safety &amp; spend</h2>
  <div class="cards">
    <div class="card kill">
      <div class="lbl">Kill switch</div>
      <div id="kill-state" class="num">…</div>
      <div id="kill-meta"><small></small></div>
      <div style="margin-top:0.5rem">
        <input id="kill-reason" type="text" placeholder="reason (optional)" style="width:60%" />
        <button id="kill-engage">engage</button>
        <button id="kill-release">release</button>
      </div>
    </div>
    <div class="card"><div class="num" id="t-spend-today">…</div><div class="lbl">Spend today (USD)</div>
      <div class="bar"><div id="b-spend-today" style="width:0%"></div></div>
      <small id="lim-daily-label"></small>
    </div>
    <div class="card"><div class="num" id="t-spend-month">…</div><div class="lbl">Spend this month (USD)</div>
      <div class="bar"><div id="b-spend-month" style="width:0%"></div></div>
      <small id="lim-monthly-label"></small>
    </div>
    <div class="card"><div class="num" id="t-disk">…</div><div class="lbl">data/ usage</div>
      <div class="bar"><div id="b-disk" style="width:0%"></div></div>
      <small id="lim-disk-label"></small>
    </div>
  </div>

  <details open>
    <summary><strong>Spend by provider</strong> &nbsp;<small>today &amp; this month, with per-provider caps</small></summary>
    <table id="provider-spend" style="margin-top:0.5rem">
      <thead><tr>
        <th>Provider</th>
        <th>Calls today</th>
        <th>$ today</th>
        <th>Daily cap</th>
        <th>Calls month</th>
        <th>$ month</th>
        <th>Monthly cap</th>
      </tr></thead>
      <tbody><tr><td colspan="7"><small>loading…</small></td></tr></tbody>
    </table>
    <small style="display:block; margin-top:0.4rem; color:#666">
      Set a cap to 0 to disable that provider's specific limit (the global daily/monthly still applies).
    </small>
  </details>

  <details>
    <summary><strong>Cost-tracking accuracy</strong> &nbsp;<small>pricing source &amp; uncosted calls</small></summary>
    <p><small id="cost-audit-meta">open to load…</small></p>
    <table id="cost-bymodel">
      <thead><tr><th>Provider</th><th>Model</th><th>Calls</th><th>$ total</th><th>Tokens in/out</th></tr></thead>
      <tbody></tbody>
    </table>
    <small style="display:block; margin-top:0.4rem; color:#666">
      Cost = real token counts from each API response × a per-model price table (cached-input discount applied).
      &ldquo;Uncosted&rdquo; = a successful call whose model isn't in the price table (would undercount the bill).
      Local/Ollama calls are correctly $0.
    </small>
  </details>

  <details>
    <summary><strong>Edit limits</strong> &nbsp;<small>persisted to data/limits.json</small></summary>
    <div style="margin: 0.6rem 0; display: flex; gap: 0.8rem; flex-wrap: wrap; align-items: flex-end;">
      <label>Daily USD <input id="lim-daily" type="number" step="0.5" min="0" style="width:6em" /></label>
      <label>Monthly USD <input id="lim-monthly" type="number" step="5" min="0" style="width:6em" /></label>
      <label>Storage GB <input id="lim-storage" type="number" step="1" min="0" style="width:6em" /></label>
      <label><input id="lim-block" type="checkbox" /> block when over</label>
      <button id="lim-save">save</button>
      <small id="lim-saved"></small>
    </div>
    <div style="margin: 0.6rem 0; padding-top: 0.4rem; border-top: 1px dashed #ccc;">
      <small style="display:block; margin-bottom:0.4rem; color:#666">
        Per-provider caps (USD). 0 = no provider-specific cap.
      </small>
      <div style="display: flex; gap: 1.2rem; flex-wrap: wrap; align-items: flex-end;">
        <fieldset style="border:1px solid #ddd; padding:0.4rem 0.8rem">
          <legend>OpenAI</legend>
          <label>daily <input id="lim-daily-openai" type="number" step="0.5" min="0" style="width:5em" /></label>
          <label style="margin-left:0.6rem">monthly <input id="lim-monthly-openai" type="number" step="5" min="0" style="width:5em" /></label>
        </fieldset>
        <fieldset style="border:1px solid #ddd; padding:0.4rem 0.8rem">
          <legend>Cohere</legend>
          <label>daily <input id="lim-daily-cohere" type="number" step="0.5" min="0" style="width:5em" /></label>
          <label style="margin-left:0.6rem">monthly <input id="lim-monthly-cohere" type="number" step="5" min="0" style="width:5em" /></label>
        </fieldset>
        <fieldset style="border:1px solid #ddd; padding:0.4rem 0.8rem">
          <legend>Anthropic</legend>
          <label>daily <input id="lim-daily-anthropic" type="number" step="0.5" min="0" style="width:5em" /></label>
          <label style="margin-left:0.6rem">monthly <input id="lim-monthly-anthropic" type="number" step="5" min="0" style="width:5em" /></label>
        </fieldset>
      </div>
    </div>
  </details>

  <h2>Memory &amp; CPU <small>— live every 3 s · hover any label for what it means</small></h2>
  <div class="cards">
    <div class="card"><div class="num" id="m-total">…</div><div class="lbl" title="Total physical RAM. On Apple Silicon this is unified memory shared with the GPU.">Total RAM</div></div>
    <div class="card"><div class="num" id="m-proj">…</div><div class="lbl" title="Private (USS) memory of ALL Polarius processes combined: backend + Docker + Node + Streamlit.">Project memory</div>
      <div class="bar"><div id="b-proj" style="width:0%"></div></div>
      <small id="m-proj-extra"></small>
    </div>
    <div class="card"><div class="num" id="m-avail">…</div><div class="lbl" title="RAM not currently in use — headroom for e.g. a local Ollama model.">Available</div></div>
    <div class="card"><div class="num" id="r-cpu">…</div><div class="lbl" title="System-wide CPU use across all cores.">CPU %</div>
      <div class="bar"><div id="b-cpu" style="width:0%"></div></div>
      <small id="r-cpu-extra"></small>
    </div>
  </div>
  <table id="mem-seg">
    <thead><tr>
      <th>Component</th>
      <th title="Unique Set Size — memory private to this component. Summing USS does NOT double-count shared libraries, so this is the accurate footprint.">USS (private)</th>
      <th title="Resident Set Size — physical RAM incl. shared pages. Overcounts when summed across processes.">RSS</th>
      <th>% RAM</th><th>Procs</th>
    </tr></thead>
    <tbody><tr><td colspan="5"><small>loading…</small></td></tr></tbody>
  </table>
  <p><small id="mem-note"></small></p>
  <div class="cards" style="margin-top:0.6rem">
    <div class="card"><div class="num" id="m-track">…</div><div class="lbl" title="Memory used by THIS monitoring process — the FastAPI backend serving this dashboard. (Also included inside Backend API above.)">Monitoring overhead</div></div>
    <div class="card" id="card-swap"><div class="num" id="r-swap">…</div><div class="lbl" title="Swap = disk used as overflow when RAM fills up. Zero is ideal; sustained non-zero means memory pressure and slowdowns.">Swap used</div><small id="r-swap-extra"></small></div>
    <div class="card"><div class="num" id="r-load">…</div><div class="lbl" title="Load average: processes wanting CPU over 1/5/15 min. Above the core count = saturation.">Load avg</div><small id="r-load-extra"></small></div>
    <div class="card" id="card-pressure"><div class="num" id="r-pressure">…</div><div class="lbl" title="Overall memory pressure from RAM % and swap use.">Memory pressure</div></div>
  </div>

  <h2>Storage <small>— where disk goes · open a panel to load (walk cached ~45 s)</small></h2>
  <details>
    <summary><strong>Project storage segmentation</strong> &nbsp;<small>DB · code · deps · data · dumps · logs · git</small></summary>
    <table id="storageseg">
      <thead><tr><th>Bucket</th><th>Size</th><th>%</th><th>Files</th><th>Note</th></tr></thead>
      <tbody><tr><td colspan="5"><small>open to load…</small></td></tr></tbody>
    </table>
    <p><small id="storageseg-total"></small></p>
  </details>
  <details>
    <summary><strong>Database tables</strong> &nbsp;<small>largest by total size (pg_total_relation_size)</small></summary>
    <table id="dbtables">
      <thead><tr><th>Table</th><th>Size</th></tr></thead>
      <tbody><tr><td colspan="2"><small>open to load…</small></td></tr></tbody>
    </table>
  </details>
  <details>
    <summary><strong>data/ &amp; logs/ per-directory</strong></summary>
    <table id="storage">
      <thead><tr><th>Path</th><th>Size</th><th>Files</th><th>Depth</th></tr></thead>
      <tbody><tr><td colspan="4"><small>loading…</small></td></tr></tbody>
    </table>
  </details>

  <h2>Corpus totals</h2>
  <div class="cards" id="cards">
    <div class="card"><div class="num" id="t-docs">…</div><div class="lbl">Documents</div></div>
    <div class="card"><div class="num" id="t-prov">…</div><div class="lbl">Provisions</div></div>
    <div class="card"><div class="num" id="t-xref">…</div><div class="lbl">Cross-references</div></div>
    <div class="card"><div class="num" id="t-sent">…</div><div class="lbl">Sentences</div></div>
    <div class="card"><div class="num" id="t-defs">…</div><div class="lbl">Defined terms</div></div>
    <div class="card"><div class="num" id="t-pdfs">…</div><div class="lbl">ČNB PDFs on disk</div></div>
  </div>

  <h2>Acceptance gates (Day-4 ingestion + Day-5 NLP)</h2>
  <div class="acceptance" id="acceptance"></div>

  <h2>Recently ingested documents</h2>
  <table id="recent">
    <thead><tr><th>Publisher</th><th>External ID</th><th>Title</th><th>Provisions</th><th>Fetched</th></tr></thead>
    <tbody><tr><td colspan="5"><small>loading…</small></td></tr></tbody>
  </table>

  <h2>System log</h2>
  <p><small>
    Tails <code id="logpath">logs/app.log</code>.
    Older history persists in the file even across server restarts.
  </small></p>
  <div class="log-controls">
    <label>Level
      <select id="f-level">
        <option value="">all</option>
        <option value="info">info</option>
        <option value="warning">warning</option>
        <option value="error">error</option>
      </select>
    </label>
    <label>Search <input id="f-q" type="search" placeholder="text or code…" /></label>
    <label><input id="f-pause" type="checkbox" /> pause</label>
    <span class="spacer"></span>
    <span><small id="log-meta"></small></span>
    <button id="clear">clear</button>
  </div>
  <div id="logs"><div class="row"><small>connecting…</small></div></div>

<script>
const $ = (id) => document.getElementById(id);
const STATS_INTERVAL = 5000;
const LOGS_INTERVAL = 2000;
const SYS_INTERVAL = 3000;

function fmt(n) { return new Intl.NumberFormat().format(n); }
function fmtBytes(b) {
  if (!b && b !== 0) return '–';
  const u = ['B','KB','MB','GB','TB']; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(b >= 100 || i === 0 ? 0 : 1) + ' ' + u[i];
}
function setBar(elId, pct, danger) {
  const el = $(elId);
  el.style.width = Math.min(100, Math.max(0, pct)) + '%';
  el.classList.remove('over','warn');
  if (pct >= 90 || danger) el.classList.add('over');
  else if (pct >= 75) el.classList.add('warn');
}

async function refreshSystem() {
  try {
    const r = await fetch('/admin/api/system');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();

    // Kill switch
    const k = d.kill_switch || {};
    const kCard = $('kill-state').closest('.card');
    if (k.active) {
      $('kill-state').textContent = '⛔ ENGAGED';
      $('kill-state').classList.add('engaged');
      kCard.classList.add('engaged');
      $('kill-meta').innerHTML = `<small>since ${(k.engaged_at||'').replace('T',' ').slice(0,19)} · reason: ${k.reason||'(none)'}</small>`;
      $('killbanner').style.display = 'block';
      $('killbanner').textContent = `Kill switch ENGAGED — ${k.reason || 'manual'}`;
    } else {
      $('kill-state').textContent = '✓ clear';
      $('kill-state').classList.remove('engaged');
      kCard.classList.remove('engaged');
      $('kill-meta').innerHTML = `<small>flag file: <code>${k.path||''}</code></small>`;
      $('killbanner').style.display = 'none';
    }

    // Spend
    const sp = d.spend || {}; const lim = d.limits || {};
    $('t-spend-today').textContent = '$' + (sp.today_usd||0).toFixed(2);
    $('t-spend-month').textContent = '$' + (sp.month_usd||0).toFixed(2);
    const dailyPct  = lim.daily_usd   > 0 ? 100 * (sp.today_usd||0) / lim.daily_usd   : 0;
    const monthPct  = lim.monthly_usd > 0 ? 100 * (sp.month_usd||0) / lim.monthly_usd : 0;
    setBar('b-spend-today', dailyPct);
    setBar('b-spend-month', monthPct);
    $('lim-daily-label').textContent   = lim.daily_usd   ? `limit $${lim.daily_usd}`   : 'no limit';
    $('lim-monthly-label').textContent = lim.monthly_usd ? `limit $${lim.monthly_usd}` : 'no limit';

    // Disk
    const st = d.storage || {};
    $('t-disk').textContent = fmtBytes(st.data_dir_bytes);
    setBar('b-disk', st.percent || 0, st.over_limit);
    $('lim-disk-label').textContent = lim.storage_gb ? `limit ${lim.storage_gb} GB` : 'no limit';

    // Memory & CPU
    const res = d.resources || {};
    const cpu = res.cpu || {};
    $('r-cpu').textContent = (cpu.percent ?? 0) + '%';
    setBar('b-cpu', cpu.percent || 0);
    $('r-cpu-extra').textContent = `${cpu.count} cores`;
    $('r-load').textContent = (cpu.load_1m ?? 0).toFixed(2);
    $('r-load-extra').textContent = `5m ${(cpu.load_5m ?? 0).toFixed(2)} · 15m ${(cpu.load_15m ?? 0).toFixed(2)}`;

    const msg = d.memory_segmentation || {};
    const msys = msg.system || {}; const swp = msg.swap || {}; const pt = msg.project_total || {};
    $('m-total').textContent = fmtBytes(msys.total_bytes);
    $('m-avail').textContent = fmtBytes(msys.available_bytes);
    $('m-proj').textContent = fmtBytes(pt.uss_bytes);
    setBar('b-proj', pt.percent_of_ram || 0);
    $('m-proj-extra').textContent = `${pt.percent_of_ram || 0}% of RAM · system ${msys.percent || 0}% used`;
    $('m-track').textContent = fmtBytes((msg.tracking || {}).uss_bytes);
    $('r-swap').textContent = fmtBytes(swp.used_bytes);
    $('r-swap-extra').textContent = swp.total_bytes ? `${swp.percent || 0}% of ${fmtBytes(swp.total_bytes)}` : 'no swap';
    $('r-pressure').textContent = msg.pressure || '–';
    $('card-pressure').style.background = msg.pressure === 'high' ? '#fdf3f0' : (msg.pressure === 'elevated' ? '#fffbe6' : '');
    $('card-swap').style.background = (swp.used_bytes > 0 && msg.pressure !== 'normal') ? '#fffbe6' : '';
    $('mem-note').textContent = msg.metric_note || '';

    const segBody = $('mem-seg').querySelector('tbody');
    const groups = msg.groups || [];
    if (!groups.length) {
      segBody.innerHTML = '<tr><td colspan="5"><small>no project processes detected</small></td></tr>';
    } else {
      segBody.innerHTML = '';
      for (const g of groups) {
        const approx = g.uss_exact === false ? ' <small title="some processes denied USS; RSS used as fallback">≈</small>' : '';
        const tr = document.createElement('tr');
        tr.innerHTML = `<td><strong>${g.label}</strong></td>
          <td>${fmtBytes(g.uss_bytes)}${approx}</td>
          <td>${fmtBytes(g.rss_bytes)}</td>
          <td>${g.percent_of_ram}%</td>
          <td>${g.n_procs}</td>`;
        segBody.appendChild(tr);
      }
      const tot = document.createElement('tr');
      tot.style.borderTop = '2px solid #bbb';
      tot.innerHTML = `<td><strong>Project total</strong></td>
        <td><strong>${fmtBytes(pt.uss_bytes)}</strong></td>
        <td>${fmtBytes(pt.rss_bytes)}</td>
        <td><strong>${pt.percent_of_ram}%</strong></td><td></td>`;
      segBody.appendChild(tot);
    }

    // Limits editor (only fill if not actively editing)
    if (document.activeElement?.id !== 'lim-daily')   $('lim-daily').value   = lim.daily_usd;
    if (document.activeElement?.id !== 'lim-monthly') $('lim-monthly').value = lim.monthly_usd;
    if (document.activeElement?.id !== 'lim-storage') $('lim-storage').value = lim.storage_gb;
    $('lim-block').checked = !!lim.block_when_over;
    for (const p of ['openai','cohere','anthropic']) {
      const d_id = 'lim-daily-' + p, m_id = 'lim-monthly-' + p;
      if (document.activeElement?.id !== d_id) $(d_id).value = lim['daily_'+p+'_usd'] || 0;
      if (document.activeElement?.id !== m_id) $(m_id).value = lim['monthly_'+p+'_usd'] || 0;
    }

    // Provider spend table
    renderProviderSpend(sp, lim);
  } catch (e) { console.error(e); }
}

function renderProviderSpend(sp, lim) {
  const tbody = $('provider-spend').querySelector('tbody');
  const today = sp.today_by_provider || [];
  const month = sp.month_by_provider || [];
  // Index month rows by provider for join
  const monthByProv = {};
  for (const m of month) monthByProv[m.provider] = m;
  const providers = today.length ? today : month;
  if (!providers.length) {
    tbody.innerHTML = '<tr><td colspan="7"><small>no API calls yet</small></td></tr>';
    return;
  }
  tbody.innerHTML = '';
  for (const row of providers) {
    const p = row.provider;
    const m = monthByProv[p] || { n_calls: 0, cost_usd: 0 };
    const dcap = lim['daily_'+p+'_usd'] || 0;
    const mcap = lim['monthly_'+p+'_usd'] || 0;
    const overD = dcap > 0 && row.cost_usd > dcap;
    const overM = mcap > 0 && m.cost_usd  > mcap;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${p}</strong></td>
      <td>${fmt(row.n_calls || 0)}</td>
      <td style="${overD?'color:#c00;font-weight:bold':''}">$${(row.cost_usd||0).toFixed(4)}</td>
      <td><small>${dcap > 0 ? '$'+dcap.toFixed(2) : '—'}</small></td>
      <td>${fmt(m.n_calls || 0)}</td>
      <td style="${overM?'color:#c00;font-weight:bold':''}">$${(m.cost_usd||0).toFixed(4)}</td>
      <td><small>${mcap > 0 ? '$'+mcap.toFixed(2) : '—'}</small></td>
    `;
    tbody.appendChild(tr);
  }
}

async function refreshStorage() {
  try {
    const r = await fetch('/admin/api/storage?top_n=40&max_depth=3');
    if (!r.ok) return;
    const d = await r.json();
    const tbody = $('storage').querySelector('tbody');
    tbody.innerHTML = '';
    for (const e of d.entries) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><code>${e.rel_path}</code></td>
        <td>${fmtBytes(e.size_bytes)}</td>
        <td>${fmt(e.n_files)}</td><td>${e.depth}</td>`;
      tbody.appendChild(tr);
    }
  } catch (e) { console.error(e); }
}

$('kill-engage').addEventListener('click', async () => {
  const reason = $('kill-reason').value || 'manual via dashboard';
  await fetch('/admin/api/killswitch/engage', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify({reason}),
  });
  refreshSystem();
});
$('kill-release').addEventListener('click', async () => {
  await fetch('/admin/api/killswitch/release', { method: 'POST' });
  refreshSystem();
});
$('lim-save').addEventListener('click', async () => {
  const patch = {
    daily_usd: parseFloat($('lim-daily').value),
    monthly_usd: parseFloat($('lim-monthly').value),
    storage_gb: parseInt($('lim-storage').value, 10),
    block_when_over: $('lim-block').checked,
  };
  for (const p of ['openai','cohere','anthropic']) {
    const d = parseFloat($('lim-daily-' + p).value);
    const m = parseFloat($('lim-monthly-' + p).value);
    if (!isNaN(d)) patch['daily_' + p + '_usd'] = d;
    if (!isNaN(m)) patch['monthly_' + p + '_usd'] = m;
  }
  const r = await fetch('/admin/api/limits', {
    method: 'POST', headers: {'content-type':'application/json'},
    body: JSON.stringify(patch),
  });
  if (r.ok) {
    $('lim-saved').textContent = 'saved ✓';
    setTimeout(() => { $('lim-saved').textContent = ''; }, 1500);
    refreshSystem();
  }
});
// --- Storage segmentation + cost audit (lazy, on panel open) ---
async function renderStorageSeg() {
  try {
    const r = await fetch('/admin/api/storage-segmentation');
    if (!r.ok) return;
    const d = await r.json();
    const tb = $('storageseg').querySelector('tbody'); tb.innerHTML = '';
    for (const b of d.buckets) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><strong>${b.label}</strong></td>
        <td>${fmtBytes(b.size_bytes)}</td><td>${b.percent}%</td>
        <td>${fmt(b.n_files || 0)}</td><td><small>${b.note || ''}</small></td>`;
      tb.appendChild(tr);
    }
    $('storageseg-total').textContent = `Total tracked on disk: ${fmtBytes(d.grand_total_bytes)} · root ${d.root}`;
    const dbt = $('dbtables').querySelector('tbody'); dbt.innerHTML = '';
    for (const t of (d.db_tables || [])) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td><code>${t.table}</code></td><td>${fmtBytes(t.size_bytes)}</td>`;
      dbt.appendChild(tr);
    }
  } catch (e) { console.error(e); }
}
async function renderCostAudit() {
  try {
    const r = await fetch('/admin/api/cost-audit');
    if (!r.ok) return;
    const d = await r.json();
    $('cost-audit-meta').innerHTML =
      `Pricing basis: <strong>${d.pricing_as_of}</strong>. ` +
      `Uncosted successful calls (unknown model, tokens&gt;0): ` +
      `<strong style="color:${d.uncosted_calls ? '#c00' : '#2a6'}">${d.uncosted_calls}</strong> of ${fmt(d.total_calls)}.`;
    const tb = $('cost-bymodel').querySelector('tbody'); tb.innerHTML = '';
    for (const m of d.by_model) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${m.provider}</td><td><code>${m.model}</code></td>
        <td>${fmt(m.n)}</td><td>$${m.cost_usd.toFixed(4)}</td>
        <td>${fmt(m.tok_in)} / ${fmt(m.tok_out)}</td>`;
      tb.appendChild(tr);
    }
  } catch (e) { console.error(e); }
}
// Lazy-load panels on open.
document.querySelector('details:has(#storage)')?.addEventListener('toggle', (e) => { if (e.target.open) refreshStorage(); });
document.querySelector('details:has(#storageseg)')?.addEventListener('toggle', (e) => { if (e.target.open) renderStorageSeg(); });
document.querySelector('details:has(#dbtables)')?.addEventListener('toggle', (e) => { if (e.target.open) renderStorageSeg(); });
document.querySelector('details:has(#cost-bymodel)')?.addEventListener('toggle', (e) => { if (e.target.open) renderCostAudit(); });

async function refreshStats() {
  try {
    const r = await fetch('/admin/api/stats');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    $('t-docs').textContent = fmt(d.totals.documents);
    $('t-prov').textContent = fmt(d.totals.provisions);
    $('t-xref').textContent = fmt(d.totals.cross_references);
    $('t-sent').textContent = fmt(d.totals.sentences ?? 0);
    $('t-defs').textContent = fmt(d.totals.definitions ?? 0);
    $('t-pdfs').textContent = fmt(d.totals.cnb_pdfs_on_disk);

    // Acceptance gates
    const accDiv = $('acceptance');
    accDiv.innerHTML = '';
    const gates = [
      { key: 'eurlex', label: 'EUR-Lex documents' },
      { key: 'sbirka', label: 'Sbírka zákonů documents' },
      { key: 'cnb',    label: 'ČNB methodologies' },
    ];
    for (const g of gates) {
      const a = d.acceptance[g.key];
      const pass = a.actual >= a.target;
      const pct = Math.min(100, Math.round(a.actual / a.target * 100));
      const div = document.createElement('div');
      div.className = 'accept-row ' + (pass ? 'pass' : 'fail');
      div.innerHTML = `<strong>${g.label}</strong><br>
        <span class="${pass ? 'ok' : 'miss'}">${a.actual} / ${a.target}</span>
        <div class="bar"><div style="width:${pct}%"></div></div>
        <small>${pass ? 'gate met' : `need ${a.target - a.actual} more`}</small>`;
      accDiv.appendChild(div);
    }
    // Range-style gates (provisions / sentences / definitions)
    const ranges = [
      { key: 'provisions',  label: 'Total provisions' },
      { key: 'sentences',   label: 'Sentence rows (Day 5)' },
      { key: 'definitions', label: 'Defined terms (Day 5)' },
    ];
    for (const g of ranges) {
      const p = d.acceptance[g.key];
      if (!p) continue;
      const pass = p.actual >= p.target_low;
      const pct = Math.min(100, Math.round(p.actual / p.target_high * 100));
      const div = document.createElement('div');
      div.className = 'accept-row ' + (pass ? 'pass' : 'fail');
      div.innerHTML = `<strong>${g.label}</strong><br>
        <span class="${pass ? 'ok' : 'miss'}">${fmt(p.actual)}</span>
        <small> (target ${fmt(p.target_low)}–${fmt(p.target_high)})</small>
        <div class="bar"><div style="width:${pct}%"></div></div>`;
      accDiv.appendChild(div);
    }

    // Recent docs
    const tbody = $('recent').querySelector('tbody');
    tbody.innerHTML = '';
    for (const r of d.recent_documents) {
      const tr = document.createElement('tr');
      const dt = r.fetched_at ? new Date(r.fetched_at).toLocaleString() : '-';
      tr.innerHTML = `<td>${r.publisher}</td><td>${r.external_id}</td>
        <td>${r.title || ''}</td><td>${fmt(r.n_provisions)}</td><td>${dt}</td>`;
      tbody.appendChild(tr);
    }
  } catch (e) {
    console.error(e);
  }
}

let logSeen = new Set();
async function refreshLogs() {
  if ($('f-pause').checked) return;
  try {
    const params = new URLSearchParams();
    if ($('f-level').value) params.set('level', $('f-level').value);
    if ($('f-q').value) params.set('q', $('f-q').value);
    params.set('limit', '300');
    const r = await fetch('/admin/api/logs?' + params.toString());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    $('logpath').textContent = d.log_path || 'logs/app.log';
    $('log-meta').textContent = `${d.entries.length} entries / ${d.total_scanned || 0} scanned`;

    const box = $('logs');
    box.innerHTML = '';
    for (const e of d.entries) {
      const row = document.createElement('div');
      row.className = 'row ' + (e.level || '');
      const ts = (e.timestamp || '').replace('T', ' ').replace('Z', '');
      const msg = e.friendly_message || e.diagnostic_message || e.code || '(no message)';
      row.innerHTML = `<span class="ts">${ts}</span>  ` +
                      `<span class="code">${e.code || ''}</span>  ` +
                      `${msg}`;
      box.appendChild(row);
    }
    if (!$('f-pause').checked) box.scrollTop = box.scrollHeight;
  } catch (e) {
    $('logs').innerHTML = `<div class="row error">⚠ log fetch failed: ${e.message}</div>`;
  }
}

$('clear').addEventListener('click', () => { $('logs').innerHTML = ''; });
$('f-level').addEventListener('change', refreshLogs);
$('f-q').addEventListener('input', () => { clearTimeout(window._fqT);
  window._fqT = setTimeout(refreshLogs, 250); });

refreshStats();  setInterval(refreshStats,  STATS_INTERVAL);
refreshLogs();   setInterval(refreshLogs,   LOGS_INTERVAL);
refreshSystem(); setInterval(refreshSystem, SYS_INTERVAL);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Schema viewer page: fetches /admin/api/schema?format=markdown and renders it with a minimal
# client-side markdown formatter (just enough for headings, tables, code spans and links).
# ---------------------------------------------------------------------------
_SCHEMA_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>RegSuper — DB schema</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { font-family: system-ui, -apple-system, sans-serif; color: #111; }
    body { max-width: 1200px; margin: 1.5rem auto; padding: 0 1.5rem; line-height: 1.5; }
    nav { display: flex; gap: 1rem; padding-bottom: 0.8rem; border-bottom: 1px solid #ddd; margin-bottom: 1rem; }
    nav a { color: #2a6; text-decoration: none; font-weight: 600; }
    h1 { margin-top: 0.5rem; }
    h2 { margin-top: 2rem; padding-top: 0.4rem; border-top: 1px solid #ddd; }
    h2 a { color: #111; text-decoration: none; }
    table { border-collapse: collapse; width: 100%; font-size: 0.9em; margin: 0.6rem 0; }
    th, td { border: 1px solid #ddd; padding: 0.3rem 0.5rem; text-align: left; vertical-align: top; }
    th { background: #fafafa; }
    code { background: #f1f1f1; padding: 0 0.25rem; border-radius: 3px; }
    .toolbar { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
    button, input { font: inherit; padding: 0.35rem 0.7rem; }
    #toc { columns: 2; padding-left: 1.2rem; }
  </style>
</head>
<body>
  <nav>
    <a href="/admin/">Dashboard</a>
    <a href="/admin/cnb">ČNB Upload</a>
    <a href="/admin/schema">DB schema</a>
    <a href="/admin/calls">API calls</a>
  </nav>
  <h1>Database schema</h1>
  <div class="toolbar">
    <input id="filter" type="search" placeholder="filter tables/columns…" style="width:18em" />
    <button id="copy">copy markdown</button>
    <a id="download" href="/admin/api/schema?format=markdown" target="_blank">raw JSON / markdown</a>
  </div>
  <div id="content"><em>loading…</em></div>

<script>
const $ = (id) => document.getElementById(id);

// Minimal markdown renderer — handles headings, paragraphs, lists, tables,
// inline code, links, italics. Enough for what render_markdown() emits.
function mdToHtml(md) {
  const lines = md.split('\\n');
  let html = ''; let i = 0;
  const inline = (s) => s
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
    .replace(/_([^_]+)_/g, '<em>$1</em>')
    .replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2">$1</a>');

  while (i < lines.length) {
    const ln = lines[i];
    if (/^# /.test(ln))      { html += `<h1>${inline(ln.slice(2))}</h1>`; i++; continue; }
    if (/^## /.test(ln))     { html += `<h2>${inline(ln.slice(3))}</h2>`; i++; continue; }
    if (/^### /.test(ln))    { html += `<h3>${inline(ln.slice(4))}</h3>`; i++; continue; }
    if (/^- /.test(ln)) {
      html += '<ul>';
      while (i < lines.length && /^- /.test(lines[i])) {
        html += `<li>${inline(lines[i].slice(2))}</li>`; i++;
      }
      html += '</ul>'; continue;
    }
    // Table: header + separator + rows
    if (/^\\|/.test(ln) && i + 1 < lines.length && /^\\|[\\s|:-]+\\|$/.test(lines[i+1])) {
      const head = ln.split('|').slice(1, -1).map(s => s.trim());
      i += 2;
      html += '<table><thead><tr>' + head.map(h => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>';
      while (i < lines.length && /^\\|/.test(lines[i])) {
        const cells = lines[i].split('|').slice(1, -1).map(s => s.trim());
        html += '<tr>' + cells.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>';
        i++;
      }
      html += '</tbody></table>'; continue;
    }
    if (ln.trim() === '') { html += ''; i++; continue; }
    html += `<p>${inline(ln)}</p>`; i++;
  }
  return html;
}

let raw = '';
async function load() {
  const r = await fetch('/admin/api/schema?format=markdown');
  const d = await r.json();
  raw = d.markdown;
  $('content').innerHTML = mdToHtml(raw);
}
$('copy').addEventListener('click', async () => {
  await navigator.clipboard.writeText(raw);
  $('copy').textContent = 'copied ✓';
  setTimeout(() => $('copy').textContent = 'copy markdown', 1200);
});
$('filter').addEventListener('input', () => {
  const q = $('filter').value.toLowerCase();
  if (!q) { $('content').innerHTML = mdToHtml(raw); return; }
  // Filter at the section level: keep ## section if any line contains q.
  const sections = raw.split(/(?=^## )/m);
  const kept = sections.filter(s => s.toLowerCase().includes(q));
  $('content').innerHTML = mdToHtml((sections[0] || '') + kept.slice(1).join(''));
});
load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# API call inspector page: list every recorded api_call, view one in detail and replay it with
# optional changes. The raw request JSON is shown in a textarea so anything can be edited (model,
# temperature, messages, max_tokens, response_format).
# ---------------------------------------------------------------------------
_CALLS_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>RegSuper — API calls</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root { font-family: system-ui, -apple-system, sans-serif; color: #111; }
    body { max-width: 1500px; margin: 1.5rem auto; padding: 0 1.5rem; line-height: 1.45; }
    nav { display: flex; gap: 1rem; padding-bottom: 0.8rem; border-bottom: 1px solid #ddd; margin-bottom: 1rem; }
    nav a { color: #2a6; text-decoration: none; font-weight: 600; }
    h1 { margin: 0.3rem 0; }
    .layout { display: grid; grid-template-columns: 1fr 1.4fr; gap: 1.5rem; }
    @media (max-width: 1100px) { .layout { grid-template-columns: 1fr; } }
    .toolbar { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; flex-wrap: wrap; align-items: center; }
    table { border-collapse: collapse; width: 100%; font-size: 0.86em; }
    th, td { border-bottom: 1px solid #eee; padding: 0.3rem 0.4rem; text-align: left; vertical-align: top; }
    th { background: #fafafa; position: sticky; top: 0; }
    tr.row { cursor: pointer; }
    tr.row:hover { background: #f4f9ff; }
    tr.row.sel { background: #eef9f3; }
    .status-ok    { color: #2a6; font-weight: 600; }
    .status-error { color: #b53; font-weight: 600; }
    pre { background: #111; color: #cfe; padding: 0.7rem; border-radius: 6px;
          font: 0.82em ui-monospace, Consolas, monospace; overflow: auto;
          max-height: 320px; white-space: pre-wrap; word-break: break-word; }
    textarea { width: 100%; min-height: 220px; font: 0.85em ui-monospace, Consolas, monospace;
               padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; }
    .pill { display: inline-block; padding: 0.1rem 0.5rem; font-size: 0.78em;
            background: #eef; color: #224; border-radius: 999px; margin-right: 0.3rem; }
    .panel { background: #f7f7f7; border: 1px solid #e3e3e3; border-radius: 8px; padding: 0.8rem 1rem; }
    button, select, input { font: inherit; padding: 0.3rem 0.6rem; }
    .err { color: #b53; }
    .ok  { color: #2a6; }
    .meta b { display: inline-block; min-width: 7em; color: #666; }
  </style>
</head>
<body>
  <nav>
    <a href="/admin/">Dashboard</a>
    <a href="/admin/cnb">ČNB Upload</a>
    <a href="/admin/schema">DB schema</a>
    <a href="/admin/calls">API calls</a>
  </nav>
  <h1>API call inspector</h1>
  <p><small>Every call to OpenAI passes through the tracker and is logged here. Click a row to view raw request + response; edit the request and "Replay" to re-run.</small></p>

  <div class="layout">
    <section>
      <div class="toolbar">
        <label>source <select id="f-source"><option value="">(all)</option></select></label>
        <label>status <select id="f-status">
          <option value="">(all)</option>
          <option value="ok">ok</option>
          <option value="error">error</option>
        </select></label>
        <label>limit <input id="f-limit" type="number" value="80" min="10" max="500" style="width:5em" /></label>
        <button id="refresh">refresh</button>
        <span id="meta"><small></small></span>
      </div>
      <div style="max-height: 70vh; overflow: auto; border: 1px solid #ddd; border-radius: 6px;">
        <table id="calls">
          <thead><tr>
            <th>ts</th><th>source</th><th>model</th><th>in/out</th>
            <th>$</th><th>ms</th><th>status</th>
          </tr></thead>
          <tbody><tr><td colspan="7"><small>loading…</small></td></tr></tbody>
        </table>
      </div>
    </section>

    <section>
      <div id="detail" class="panel"><em>select a call on the left.</em></div>
    </section>
  </div>

<script>
const $ = (id) => document.getElementById(id);
let selectedId = null;
let detail = null;

function pretty(x) { try { return JSON.stringify(x, null, 2); } catch { return String(x); } }
function fmtUSD(c) { return '$' + (c||0).toFixed(c < 0.01 ? 5 : 3); }

async function loadList() {
  const params = new URLSearchParams();
  if ($('f-source').value) params.set('source', $('f-source').value);
  if ($('f-status').value) params.set('status', $('f-status').value);
  params.set('limit', $('f-limit').value);
  const r = await fetch('/admin/api/calls?' + params.toString());
  if (!r.ok) { $('meta').textContent = 'load failed'; return; }
  const d = await r.json();

  // Update source filter dropdown only on first load (or when sources change).
  const sel = $('f-source');
  const have = new Set(Array.from(sel.options).map(o => o.value));
  for (const s of d.sources) {
    if (!have.has(s)) {
      const opt = document.createElement('option');
      opt.value = s; opt.textContent = s; sel.appendChild(opt);
    }
  }

  const tbody = $('calls').querySelector('tbody');
  tbody.innerHTML = '';
  for (const c of d.calls) {
    const tr = document.createElement('tr');
    tr.className = 'row' + (c.id === selectedId ? ' sel' : '');
    tr.dataset.id = c.id;
    const ts = (c.ts || '').replace('T',' ').slice(0,19);
    tr.innerHTML = `<td><small>${ts}</small></td>
      <td><span class="pill">${c.source}</span></td>
      <td>${c.model}</td>
      <td><small>${c.tokens_input}/${c.tokens_output}${c.tokens_cached?' (c'+c.tokens_cached+')':''}</small></td>
      <td>${fmtUSD(c.cost_usd)}</td>
      <td><small>${c.latency_ms}</small></td>
      <td class="status-${c.status}">${c.status==='ok'?'✓':'✗'}</td>`;
    tr.addEventListener('click', () => loadDetail(c.id));
    tbody.appendChild(tr);
  }
  $('meta').innerHTML = `<small>${d.calls.length} calls</small>`;
}

async function loadDetail(id) {
  selectedId = id;
  // re-render selection state in list
  for (const tr of document.querySelectorAll('#calls tbody tr.row')) {
    tr.classList.toggle('sel', tr.dataset.id === id);
  }
  $('detail').innerHTML = '<em>loading…</em>';
  const r = await fetch('/admin/api/calls/' + id);
  if (!r.ok) { $('detail').innerHTML = `<span class="err">load failed: ${r.status}</span>`; return; }
  detail = await r.json();
  renderDetail();
}

function renderDetail() {
  const c = detail;
  const reqStr = pretty(c.request_json);
  const respStr = pretty(c.response_json);
  $('detail').innerHTML = `
    <div class="meta">
      <p><b>id</b> <code>${c.id}</code></p>
      <p><b>ts</b> ${(c.ts||'').replace('T',' ').slice(0,19)}</p>
      <p><b>source</b> <span class="pill">${c.source}</span> <b>provider</b> ${c.provider} <b>endpoint</b> ${c.endpoint}</p>
      <p><b>model</b> <code>${c.model}</code> <b>status</b> <span class="status-${c.status}">${c.status}</span> <b>latency</b> ${c.latency_ms} ms</p>
      <p><b>tokens</b> in ${c.tokens_input} / out ${c.tokens_output} / cached ${c.tokens_cached}
         <b>cost</b> ${fmtUSD(c.cost_usd)}</p>
      ${c.error ? `<p class="err"><b>error</b> ${c.error}</p>` : ''}
      ${c.response_summary ? `<p><b>summary</b> ${c.response_summary.slice(0,500)}</p>` : ''}
    </div>
    <h3>Response</h3>
    <pre>${escapeHtml(respStr)}</pre>
    <h3>Request (edit + replay)</h3>
    <textarea id="req-edit" spellcheck="false">${escapeHtml(reqStr)}</textarea>
    <div class="toolbar" style="margin-top:0.5rem">
      <button id="replay">▶ replay</button>
      <button id="reset">reset to original</button>
      <span id="replay-status"></span>
    </div>
  `;
  $('replay').addEventListener('click', doReplay);
  $('reset').addEventListener('click', () => { $('req-edit').value = pretty(c.request_json); });
}

function escapeHtml(s) {
  return (s||'').replace(/[&<>]/g, ch => ({ '&':'&amp;','<':'&lt;','>':'&gt;' }[ch]));
}

async function doReplay() {
  let edited;
  try { edited = JSON.parse($('req-edit').value); }
  catch (e) {
    $('replay-status').innerHTML = `<span class="err">invalid JSON: ${e.message}</span>`;
    return;
  }
  $('replay-status').textContent = 'running…';
  // The replay endpoint overlays this JSON on the stored request and re-runs.
  const r = await fetch(`/admin/api/calls/${selectedId}/replay`, {
    method: 'POST',
    headers: {'content-type':'application/json'},
    body: JSON.stringify(edited),
  });
  if (!r.ok) {
    const t = await r.text();
    $('replay-status').innerHTML = `<span class="err">${r.status} ${t.slice(0,200)}</span>`;
    return;
  }
  const d = await r.json();
  $('replay-status').innerHTML = `<span class="ok">✓ ${d.summary?.slice(0,200) || 'ok'}</span>`;
  loadList(); // pick up the new replay row
}

$('refresh').addEventListener('click', loadList);
$('f-source').addEventListener('change', loadList);
$('f-status').addEventListener('change', loadList);
$('f-limit').addEventListener('change', loadList);
loadList();
setInterval(loadList, 6000);
</script>
</body>
</html>
"""
