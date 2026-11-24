"""Async HTTP client for the Pythia reasoning engine sidecar.

Every call first checks `PYTHIA_ENABLED` and `PYTHIA_URL` and raises `PythiaNotConfigured` when
Pythia is off, so the integration is strictly opt-in. All calls share one pooled HTTP client and
pass their own per-request timeout. Transport errors propagate untranslated: `api/pythia.py` is
the single place that maps `httpx.HTTPError` to HTTP 502, so nothing is mapped twice.
"""

from __future__ import annotations

from typing import Any

import httpx

from regsuper.config import get_settings

# ── Shared connection pool ──────────────────────────────────────────────────────────────────────
# One process-wide client (`_CLIENT`) is reused by every call below for connection reuse and
# keep-alive: at most 20 connections, 10 kept alive, 30 s keep-alive expiry. It is created inside
# the running event loop, never at import time, because an AsyncClient binds to the loop it was
# created in. init_pool() creates it in the api/main.py lifespan before serving and close_pool()
# tears it down; where the lifespan never runs (CI, calibration/pooled.py), _pool() creates it on
# first use. Each call still passes its own timeout, so the slow answer call (about 400 s) keeps
# its long budget while the fast reads keep theirs.
_POOL_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
_CLIENT: httpx.AsyncClient | None = None


async def init_pool() -> None:
    """Create the shared pooled client; idempotent.

    Call from the running event loop; the api/main.py lifespan does so before serving. A second
    call while a pool exists is a no-op, so re-entrant lifespans and test setup are safe.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(limits=_POOL_LIMITS)


async def close_pool() -> None:
    """Close and drop the shared pooled client on app shutdown; a no-op when no pool exists."""
    global _CLIENT
    if _CLIENT is not None:
        client, _CLIENT = _CLIENT, None
        await client.aclose()


def _pool() -> httpx.AsyncClient:
    """Return the shared pooled client, creating it if the lifespan never ran.

    CI and the calibration harness call Pythia without booting the app. Lazy creation is safe
    because the caller is already inside a running event loop. If creation fails, the exception
    propagates rather than a None client being returned.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(limits=_POOL_LIMITS)
    return _CLIENT


class PythiaNotConfigured(RuntimeError):
    """Raised when a Pythia call is attempted but PYTHIA_ENABLED/PYTHIA_URL are not set."""


def _base_url() -> str:
    s = get_settings()
    if not (s.pythia_enabled and s.pythia_url):
        raise PythiaNotConfigured(
            "Pythia is disabled — set PYTHIA_ENABLED=true and PYTHIA_URL to the sidecar URL"
        )
    return s.pythia_url.rstrip("/")


async def health(*, timeout: float = 8.0) -> dict[str, Any]:
    """Return the sidecar's health payload (GET /health)."""
    r = await _pool().get(f"{_base_url()}/health", timeout=timeout)
    r.raise_for_status()
    return r.json()


async def _write_engine_llm_rows(
    result: Any,
    *,
    path: str,
    org_id: Any,
    analysis_run_id: Any,
    latency_ms: int | None,
) -> None:
    """Write one costed api_call row for each LLM usage entry the sidecar reports.

    Each `audit_trace.llm_usage` entry becomes a `provider='pythia_engine_llm'` row: the engine's
    LLM spend, kept separate from the zero-cost transport row. An empty or missing list writes
    nothing, which is the case on the default local (Ollama) synthesizer path.

    Fully guarded: a malformed payload (non-dict result, non-list usage, missing or mistyped entry
    keys) or a database or pricing failure is logged and swallowed, because the answer must still
    be returned when cost logging fails. Only token counts and model names are stored, never
    prompt or response text or secrets.
    """
    from regsuper.observability import api_tracker, pricing
    from regsuper.observability.events import emit

    try:
        if not isinstance(result, dict):
            return
        audit = result.get("audit_trace")
        if not isinstance(audit, dict):
            return
        entries = audit.get("llm_usage")
        if not isinstance(entries, list):
            return
        for entry in entries:
            try:
                if not isinstance(entry, dict):
                    continue
                model = entry.get("model")
                if not model or not isinstance(model, str):
                    continue

                def _int(e: dict, key: str) -> int:
                    val = e.get(key, 0)
                    try:
                        return max(0, int(val))
                    except (TypeError, ValueError):
                        return 0

                tokens_input = _int(entry, "tokens_input")
                tokens_output = _int(entry, "tokens_output")
                tokens_cached = _int(entry, "tokens_cached")
                cost = await pricing.estimate_cost(
                    model,
                    kind="token",
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    tokens_cached=tokens_cached,
                )
                # Per-stage cost attribution: the provider stays 'pythia_engine_llm', so spend
                # rollups keyed on provider are unaffected, and the source carries the stage
                # suffix so spend_summary can split spend by stage.
                _stage = str(entry.get("stage") or "unknown").strip() or "unknown"
                await api_tracker._persist_row(
                    provider="pythia_engine_llm",
                    model=model,
                    endpoint=path,
                    source=f"pythia.answer.{_stage}",
                    project_id=None,
                    org_id=org_id,
                    analysis_run_id=analysis_run_id,
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    tokens_cached=tokens_cached,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    status="ok",
                    error=None,
                    request_json={},
                    response_json={},
                    response_summary=None,
                )
            except Exception as exc:  # one bad entry must not skip the rest or break the call
                await emit(
                    "pythia.call.usage_row_failed",
                    module="polarius",
                    level="warn",
                    path=path,
                    detail=repr(exc)[:200],
                )
    except Exception as exc:  # log-and-continue: cost-logging must never break the answer
        await emit(
            "pythia.call.usage_failed",
            module="polarius",
            level="warn",
            path=path,
            detail=repr(exc)[:200],
        )


async def _track(path: str, fn, *, org_id: Any = None, analysis_run_id: Any = None):
    """Time a sidecar call, record a zero-cost api_call row and emit pythia.call.{ok,failed}.

    Only analysis-path calls are tracked. The control-room reads (browse, act, rulings,
    importance) are deliberately untracked, so the panel's own polling does not inflate the app's
    call count.

    `org_id` and `analysis_run_id` attribute the call's cost to the originating organisation and
    analysis run. They are written to the api_call row and are NULL when omitted.

    The `provider='pythia'` row is the transport row: one per call, always zero cost. The engine's
    LLM spend is a separate cost identity that the engine reports as `audit_trace.llm_usage`
    (per-stage {stage, model, tokens_input/output/cached} entries). Each entry becomes one costed
    `provider='pythia_engine_llm'` row, priced with the same layered price book and tagged with the
    same org_id and analysis_run_id. On the default Ollama synthesizer path the engine reports no
    usage, so no extra rows are written. That step is guarded: a malformed usage list never breaks
    the call, and the answer is returned even if cost logging fails.
    """
    import time as _time

    from regsuper.observability import api_tracker
    from regsuper.observability.events import emit

    t0 = _time.perf_counter()
    try:
        result = await fn()
        ms = int((_time.perf_counter() - t0) * 1000)
        # The engine's request id (from its audit_trace) joins the cost rows, the engine trace and
        # the X-Pythia-Request-Id header, so stamp it on the transport row.
        _rid = result.get("audit_trace", {}).get("request_id") if isinstance(result, dict) else None
        await api_tracker._persist_row(
            provider="pythia",
            model="sidecar",
            endpoint=path,
            source="pythia.proxy",
            project_id=None,
            org_id=org_id,
            analysis_run_id=analysis_run_id,
            tokens_input=0,
            tokens_output=0,
            tokens_cached=0,
            cost_usd=0.0,
            latency_ms=ms,
            status="ok",
            error=None,
            request_json={},
            response_json={},
            response_summary=(f"rid={_rid}" if _rid else None),
        )
        await _write_engine_llm_rows(
            result, path=path, org_id=org_id, analysis_run_id=analysis_run_id, latency_ms=ms
        )
        await emit("pythia.call.ok", module="polarius", path=path, latency_ms=ms)
        return result
    except Exception as exc:
        ms = int((_time.perf_counter() - t0) * 1000)
        await api_tracker._persist_row(
            provider="pythia",
            model="sidecar",
            endpoint=path,
            source="pythia.proxy",
            project_id=None,
            org_id=org_id,
            analysis_run_id=analysis_run_id,
            tokens_input=0,
            tokens_output=0,
            tokens_cached=0,
            cost_usd=0.0,
            latency_ms=ms,
            status="error",
            error=repr(exc)[:300],
            request_json={},
            response_json={},
            response_summary=None,
        )
        await emit(
            "pythia.call.failed",
            module="polarius",
            level="error",
            path=path,
            detail=repr(exc)[:200],
        )
        raise


async def retrieve(
    query: str,
    *,
    method: str = "cte",
    k: int = 20,
    jurisdiction: list[str] | None = None,
    timeout: float = 60.0,
    org_id: Any = None,
    analysis_run_id: Any = None,
) -> dict[str, Any]:
    """Run LLM-free graph retrieval on the sidecar (POST /v1/retrieve).

    Returns a PythiaAnswer dict (cited_provisions, reasoning_subgraph, ...). `org_id` and
    `analysis_run_id` tag the api_call row for cost attribution and are NULL when omitted.
    """

    async def _do() -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "k": k}
        if jurisdiction:
            body["jurisdiction"] = jurisdiction
        r = await _pool().post(
            f"{_base_url()}/v1/retrieve", params={"method": method}, json=body, timeout=timeout
        )
        r.raise_for_status()
        return r.json()

    return await _track("retrieve", _do, org_id=org_id, analysis_run_id=analysis_run_id)


async def answer(
    query: str,
    *,
    method: str = "cte",
    k: int = 12,
    max_hops: int = 3,
    as_of: str | None = None,
    verify: str | None = None,
    project_context: dict[str, Any] | None = None,
    timeout: float = 400.0,
    org_id: Any = None,
    analysis_run_id: Any = None,
) -> dict[str, Any]:
    """Request a full grounded, cited answer from the sidecar (POST /v1/answer).

    Slow: two local-LLM calls, plus a cold model load on the first request, hence the long default
    timeout. Returns a PythiaAnswer dict (drafted_answer, cited_provisions, ...).

    Args:
        as_of: ISO date for a point-in-time answer, based on the versions in force on that date.
        verify: 'off', 'gated' or 'always'; selects the sidecar's edge-assertion faithfulness pass.
        project_context: A small whitelisted typed dict of known business-profile facts
            (legal_form, nace_code, derived_tags, size bands, jurisdiction, the five gate booleans,
            municipality_code, preferred_language) that seed or override the engine's intake
            extraction. It carries no free text or personal data beyond those fields and is never
            stored in the api_call row. Callers send it only when answer_profile_context_enabled
            is on.
        org_id: Tags the api_call row for cost attribution; NULL when omitted.
        analysis_run_id: As `org_id`, for the analysis run.

    `as_of`, `verify` and `project_context` are left out of the request body when None, so the
    sidecar applies its own defaults (verify='gated', as_of=current law, no profile context).
    """

    async def _do() -> dict[str, Any]:
        # Optional pre-call spend gate (pythia_boundary_spend_gate_enabled). /v1/answer spends LLM
        # tokens in the sidecar, so with the gate on this raises before the POST, with zero spend:
        #   killswitch.assert_not_killed()                    raises KillSwitchEngaged
        #   limits.enforce_spend_limit('pythia_engine_llm')   raises SpendLimitExceeded
        # The per-provider caps checked are those of 'pythia_engine_llm', the provider the engine's
        # LLM spend is recorded under (see _track), not 'openai': the answer path never spends a
        # direct OpenAI key, so the 'openai' caps would never see this spend. The global daily and
        # monthly caps apply either way. Only answer() is gated; retrieve(), candidates() and
        # browse() make no LLM calls. With the flag off, the gate costs one settings read.
        if get_settings().pythia_boundary_spend_gate_enabled:
            from regsuper.observability import killswitch, limits

            killswitch.assert_not_killed()
            await limits.enforce_spend_limit(provider="pythia_engine_llm")
        body: dict[str, Any] = {"query": query, "method": method, "k": k, "max_hops": max_hops}
        if as_of is not None:
            body["as_of"] = as_of
        if verify is not None:
            body["verify"] = verify
        if project_context is not None:
            body["project_context"] = project_context
        r = await _pool().post(f"{_base_url()}/v1/answer", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    return await _track("answer", _do, org_id=org_id, analysis_run_id=analysis_run_id)


# ── Remote NexusPort: query-independent access to the canonical Nexus corpus ──────────────────────
async def candidates(
    *,
    jurisdiction: list[str] | None = None,
    legal_domain: list[str] | None = None,
    legal_hierarchy: list[str] | None = None,
    limit: int = 200,
    timeout: float = 30.0,
    org_id: Any = None,
    analysis_run_id: Any = None,
) -> list[dict[str, Any]]:
    """Return metadata-filtered, centrality-ranked candidate provisions (POST /v1/candidates).

    This replaces the app's L1 tag-filter step when reasoning over the canonical corpus. Returns
    provision rows (id, label, jurisdiction, legal_hierarchy, external_id, source_title,
    bindingness, temporal_status, valid_from/to, analysis_roles, ...). `org_id` and
    `analysis_run_id` tag the api_call row for cost attribution and are NULL when omitted.
    """

    async def _do() -> list[dict[str, Any]]:
        body: dict[str, Any] = {"limit": limit}
        if jurisdiction:
            body["jurisdiction"] = jurisdiction
        if legal_domain:
            body["legal_domain"] = legal_domain
        if legal_hierarchy:
            body["legal_hierarchy"] = legal_hierarchy
        r = await _pool().post(f"{_base_url()}/v1/candidates", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    return await _track("candidates", _do, org_id=org_id, analysis_run_id=analysis_run_id)


async def provisions(
    ids: list[str], *, timeout: float = 30.0, org_id: Any = None, analysis_run_id: Any = None
) -> list[dict[str, Any]]:
    """Return provision metadata and trust facets for Nexus provision ids (POST /v1/provisions).

    Input order is preserved. Used by the L2 path when law_source='nexus'. `org_id` and
    `analysis_run_id` tag the api_call row for cost attribution and are NULL when omitted.
    """
    if not ids:
        return []

    async def _do() -> list[dict[str, Any]]:
        r = await _pool().post(f"{_base_url()}/v1/provisions", json={"ids": ids}, timeout=timeout)
        r.raise_for_status()
        return r.json()

    return await _track("provisions", _do, org_id=org_id, analysis_run_id=analysis_run_id)


async def importance(ids: list[str], *, timeout: float = 30.0) -> dict[str, Any]:
    """Return structural importance per Nexus provision id (POST /v1/importance).

    Maps each id to {centrality, community}: a "these matter most" ranking prior plus regime
    membership.
    """
    if not ids:
        return {}
    r = await _pool().post(f"{_base_url()}/v1/importance", json={"ids": ids}, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── Read surface for the regulation explorer (faceted browse, act § tree, rulings panel) ──────────
async def browse(*, timeout: float = 30.0) -> dict[str, Any]:
    """Return the faceted-browse index: facets, regimes, spine and catalogue (GET /v1/browse)."""
    r = await _pool().get(f"{_base_url()}/v1/browse", timeout=timeout)
    r.raise_for_status()
    return r.json()


async def act(
    external_id: str, *, as_of: str | None = None, timeout: float = 30.0
) -> dict[str, Any]:
    """Return one act's nested § tree for drill-down (GET /v1/act/{external_id}).

    `external_id` may contain a slash (Czech act numbers such as 240/2013); the sidecar's path
    route handles it. `as_of` is an ISO date for a point-in-time view. Raises
    httpx.HTTPStatusError (404) for an unknown act, which the caller surfaces.
    """
    params = {"as_of": as_of} if as_of else None
    r = await _pool().get(f"{_base_url()}/v1/act/{external_id}", params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def rulings(
    provision_id: str, *, as_of: str | None = None, timeout: float = 30.0
) -> list[dict[str, Any]]:
    """Return the opt-in case-law panel for one statute provision (GET /v1/rulings/{provision_id}).

    Each ruling includes court, case number, title and date. Rulings are never folded into a
    statute answer; they are shown alongside it on request.
    """
    params = {"as_of": as_of} if as_of else None
    r = await _pool().get(
        f"{_base_url()}/v1/rulings/{provision_id}", params=params, timeout=timeout
    )
    r.raise_for_status()
    return r.json()


# ── Sidecar telemetry (control-room reads, not tracked as analysis calls) ─────────────────────────
async def metrics(*, timeout: float = 10.0) -> dict[str, Any]:
    """Return the sidecar's aggregate telemetry: GAR, verdict split, method mix, latency."""
    r = await _pool().get(f"{_base_url()}/metrics", timeout=timeout)
    r.raise_for_status()
    return r.json()


async def telemetry(limit: int = 50, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Return recent per-request sidecar traces for the request inspector (GET /v1/telemetry)."""
    r = await _pool().get(f"{_base_url()}/v1/telemetry", params={"limit": limit}, timeout=timeout)
    r.raise_for_status()
    return r.json()


async def nexus_stats(*, timeout: float = 20.0) -> dict[str, Any]:
    """Return Nexus corpus, freshness, graph, embedding, GNN and database statistics.

    Calls GET /v1/nexus/stats.
    """
    r = await _pool().get(f"{_base_url()}/v1/nexus/stats", timeout=timeout)
    r.raise_for_status()
    return r.json()


async def nexus_events(limit: int = 50, *, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Return recent nexus_event rows: ingest, reindex, materialize, check (/v1/nexus/events)."""
    r = await _pool().get(
        f"{_base_url()}/v1/nexus/events", params={"limit": limit}, timeout=timeout
    )
    r.raise_for_status()
    return r.json()


async def nexus_health(*, timeout: float = 8.0) -> dict[str, Any]:
    """Return the sidecar's Nexus health payload (GET /v1/nexus/health)."""
    r = await _pool().get(f"{_base_url()}/v1/nexus/health", timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── Auto-ingestion control surface: untracked reads plus one write ────────────────────────────────
# Like nexus_stats and nexus_events, these are control-room calls and are not tracked as analysis
# calls. Transport errors propagate so the admin layer can show an honest off or unreachable state;
# for the same reason the one write (run-once) lets a 409 (already running) propagate as an
# httpx.HTTPStatusError.
async def ingestion_status(*, timeout: float = 15.0) -> dict[str, Any]:
    """Return the latest auto-ingestion run, next_run_at, queue_pending and corpus metrics.

    Calls GET /v1/ingestion/status. The persisted on/off `enabled` setting is owned by the app and
    merged in by the admin layer.
    """
    r = await _pool().get(f"{_base_url()}/v1/ingestion/status", timeout=timeout)
    r.raise_for_status()
    return r.json()


async def ingestion_history(limit: int = 50, *, timeout: float = 15.0) -> dict[str, Any]:
    """Return recent ingestion_run rows for the history table (GET /v1/ingestion/history)."""
    r = await _pool().get(
        f"{_base_url()}/v1/ingestion/history", params={"limit": limit}, timeout=timeout
    )
    r.raise_for_status()
    return r.json()


async def ingestion_run_once(
    dry_run: bool | None = None, *, timeout: float = 30.0
) -> dict[str, Any]:
    """Trigger one single-flight auto-ingestion cycle (POST /v1/ingestion/run-once).

    `dry_run` is left out of the body when None, so the sidecar applies its own default (the
    auto_ingest_dry_run setting). Returns {started: true, run_id} on success. A 409
    {already_running} from the sidecar surfaces as httpx.HTTPStatusError (409) so the admin layer
    can pass it through.
    """
    body: dict[str, Any] = {}
    if dry_run is not None:
        body["dry_run"] = bool(dry_run)
    r = await _pool().post(f"{_base_url()}/v1/ingestion/run-once", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()
