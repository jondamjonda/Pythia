"""Event taxonomy and `emit()`, the app's observability event bus.

Every meaningful step in ingestion, retrieval and analysis publishes an `Event`. Events go to
two places:

1. The structlog JSON log, which is kept permanently.
2. In-process asyncio queues, drained by the server-sent event endpoints that feed the
   frontend's log panels: one channel per project, plus a system channel for the operator's
   control room.

Event codes are hierarchical and stable; `FRIENDLY_TEMPLATES` renders the user-facing text.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

from regsuper.config import get_settings
from regsuper.observability.logging_config import get_logger

_log = get_logger(__name__)


# The gap_kind backlog vocabulary, mirrored from the engine. The engine owns this vocabulary
# (pythia.reasoning.gap_kind.GAP_KIND_BACKLOG_CODES); the app pins the same eight strings in the
# same order, so the `practical.gap_backlog` telemetry and the engine's missing-data queue use one
# language. A parity test on each side (tests/api/test_gap_kind_parity.py here,
# tests/test_gap_kind_parity.py in the engine) fails on drift, so change both together.
GAP_KIND_BACKLOG_CODES: tuple[str, ...] = (
    "no_fact_row",
    "no_text_support",
    "decree_absent",
    "soft_law_absent",
    "authority_locator",
    "form_portal",
    "annual_instrument_stale",
    "extraction_not_run",
)


class Event(TypedDict):
    timestamp: str
    project_id: str | None
    # The owning subsystem, which the control room groups events by. Derived from `code` when
    # omitted. `mcp` is the Conduit channel (silent unless conduit_enabled); its events go to the
    # system channel.
    module: Literal["nexus", "pythia", "polarius", "system", "mcp"] | None
    level: Literal["info", "warn", "error"]
    code: str
    friendly_message: str
    diagnostic_message: str
    payload: dict[str, Any]


# User-facing text for each event code; payload fields fill the placeholders.
FRIENDLY_TEMPLATES: dict[str, str] = {
    "project.created": 'Project "{name}" created.',
    "analysis.started": 'Starting analysis of "{name}". This usually takes 3–8 minutes.',
    "retrieval.candidates.found": "Found {n} potentially relevant provisions.",
    "retrieval.rewrite.start": "Rewriting query for broader recall…",
    "retrieval.rewrite.done": "Generated {variants} query variants in {latency_ms} ms.",
    "retrieval.hyde.start": "Generating hypothetical answers (HyDE)…",
    "retrieval.hyde.done": "Generated {variants} hypothetical answers in {latency_ms} ms.",
    "retrieval.search.done": "Searched {n_indexes} indexes; merged {n_candidates} candidates.",
    "retrieval.rerank.done": "Reranked to top {n} with cross-encoder.",
    "retrieval.expand.done": "Expanded {n} hits with ancestor articles and binding definitions.",
    "retrieval.refused": "Question fell outside the loaded regulations; refused "
    "(rerank score {score:.2f} below threshold {threshold:.2f}).",
    "retrieval.flagged": "Answer shown with a caution: reranking was unavailable, so retrieval "
    "confidence could not be calibrated — treat as low-confidence "
    "({reason}).",
    "analysis.applicability.started": "Checking which provisions apply to your project…",
    "analysis.applicability.progress": "Reviewed {done} of {total} provisions ({applicable} applicable so far).",
    "analysis.applicability.complete": "{n} provisions are applicable.",
    "analysis.obligations.started": "Extracting specific obligations from the applicable provisions…",
    "analysis.obligations.complete": "Found {n} obligations.",
    "analysis.gaps.complete": "Compared against your uploaded documents — {n} gaps identified.",
    "analysis.complete": "Analysis complete.",
    "ingest.document.started": "Ingesting {publisher} document {external_id}…",
    "ingest.document.complete": "Ingested {publisher} document {external_id}: {n_provisions} provisions.",
    "ingest.document.failed": "Ingestion failed for {publisher} {external_id}: {detail}",
    "error.api": "Connection problem with {service}: {detail}. Retrying…",
    "error.fatal": "Stopped due to an unexpected error. Copy the Diagnostics tab and share with support.",
    # Control-room system and resource events (module="system").
    "resource.memory.elevated": "Memory use is elevated ({vm_pct:.0f}% RAM, {swap_pct:.0f}% swap).",
    "resource.memory.critical": "Memory pressure is HIGH ({vm_pct:.0f}% RAM, {swap_pct:.0f}% swap) — the host is near its limit.",
    "resource.swap.active": "The system is swapping ({swap_pct:.0f}%) — performance will degrade.",
    "resource.disk.warning": "Disk space is low ({free_gb:.1f} GB free on {path}).",
    "resource.db.pool_saturated": "Database connection pool is saturated ({used_pct:.0f}% of {limit}).",
    "resource.db.cache_degraded": "Database cache hit ratio dropped to {cache_hit_pct:.0f}%.",
    "resource.embed.running": "Embedding pass running ({done}/{total}).",
    "resource.embed.idle": "Embedding pass finished.",
    "resource.collector.error": "A system-stats collector tick failed for '{metric_class}': {detail}",
    # Control-room budget, cap and kill-switch events (module="polarius" or "system").
    "budget.key.warn": "API key {key_id} has used {pct:.0f}% of its ${cap:.2f} weekly budget.",
    "limits.spend.warned": "Weekly spend on {key_id} is at {pct:.0f}% of cap.",
    "limits.spend.exceeded": "Weekly budget for {key_id} exceeded (${spent:.2f} of ${cap:.2f}) — calls blocked ({tier}).",
    "resource.cap.refused": "A {kind} action was refused to protect the system: {reason}",
    "resource.cpu.warn": "CPU sustained high ({cpu_pct:.0f}%).",
    "killswitch.engaged": "Kill-switch ENGAGED: {reason}",
    "killswitch.released": "Kill-switch released — normal operation resumed.",
    # Control-room analysis-cascade and engine-call events (module="polarius").
    "analysis.run.started": "Analysis run started ({layers}) on the {law_source} corpus.",
    "analysis.run.complete": "Analysis complete — {applicable} applicable · {obligations} obligations · {gaps} gaps.",
    "analysis.run.failed": "Analysis run failed: {reason}",
    "analysis.law_source.refused": "Analysis refused — law_source '{law_source}' is preview-only (use regsuper).",
    "analysis.citations.summary": "Citations this run: {grounded}/{total} grounded.",
    "pythia.call.ok": "Pythia sidecar call ok ({path} · {latency_ms} ms).",
    "pythia.call.failed": "Pythia sidecar call failed ({path}): {detail}",
    # Pythia engine health, bridged by pythia_monitor (module="pythia").
    "pythia.health.ok": "Pythia sidecar reachable.",
    "pythia.health.unreachable": "Pythia sidecar unreachable ({detail}).",
    "pythia.health.ollama_up": "Pythia: local LLM (Ollama) available — drafting enabled.",
    "pythia.health.degraded": "Pythia: local LLM (Ollama) unavailable — answers will refuse (retrieval still works).",
    # Cost-model alarm (module="system"). An unknown model priced at $0 must be loud, never silent.
    "cost.model.unknown": "Unknown model '{model}' — priced at $0.00. Add it to data/model_prices.json so spend is tracked.",
    # Startup sanity check on spend caps (module="polarius", via the limits. prefix).
    "limits.suspiciously_low": "Spend cap looks suspiciously low (daily ${daily_usd:.2f}) — a test value may be throttling live runs.",
    # Cascade applicability refusal (module="polarius").
    "analysis.applicability.refused": "Provision refused — retrieval confidence {confidence:.2f} fell below the applicability floor "
    "{floor:.2f}; recorded as needs_review.",
    # Background analysis-run worker lifecycle (module="polarius", via the analysis. prefix).
    "analysis.job.queued": "Analysis run {run_id} queued.",
    "analysis.job.started": "Analysis run {run_id} started.",
    "analysis.job.progress": "Analysis run {run_id}: {stage} ({done}/{total}).",
    "analysis.job.done": "Analysis run {run_id} finished.",
    "analysis.job.failed": "Analysis run {run_id} failed: {reason}",
    "analysis.job.refused": "Analysis run {run_id} refused: {reason}",
    # Compliance guard (module="polarius").
    "compliance.answer_mode.judged": "Compliance guard classified the answer as '{answer_mode}'.",
    "compliance.refusal.projected": "Compliance guard refused (fail-closed) — {reason}.",
    # Hand-off to a qualified lawyer (module="polarius").
    "handoff.created": 'Handoff to a qualified lawyer created for project "{name}".',
    "handoff.consent.recorded": "Handoff consent recorded — contact details may now be shared.",
    # Audit export (module="polarius").
    "export.requested": "Export of {kind} {target_id} requested.",
    "export.ready": "Export {export_id} is ready to download.",
    "export.refused": "Export refused — {reason}.",
    # EU AI Act incident logging and human review (module="polarius").
    "aiact.incident.logged": "AI-Act incident logged ({severity}).",
    "finding.review_requested": "Human review requested for finding {finding_id}.",
    # Business-profile lifecycle (module="polarius").
    "profile.created": 'Business profile "{name}" created.',
    "profile.updated": "Business profile {profile_id} updated.",
    "profile.deleted": "Business profile {profile_id} deleted.",
    # Plan tier and entitlement events (module="polarius").
    "tier.upgraded": "Plan upgraded to {tier}.",
    "tier.gated": '"{feature}" needs the {required_tier} plan (current: {current_tier}).',
    "tier.quota_exceeded": "Quota for {metric} exhausted on the {tier} plan ({used}/{limit}).",
    # Answer gap telemetry (module="polarius"): one event per known-data-gap line in the engine's
    # practical block, so the most frequently missing data classes rise to the top of the backlog.
    "answer.practical.gap": "Answer surfaced a known data gap ({gap_class}).",
    # Engine claim-verification summary (module="pythia"). The engine records it in its telemetry
    # ring only when claim_verify_enabled is on. The 'answer.claims.' prefix is listed in
    # _MODULE_PREFIXES before the bare 'answer.' prefix, so it reaches the Pythia panel. Operator
    # diagnostic text, not customer-facing.
    "answer.claims.summary": "Claim-verify loop ({mode}): {claims_total} claims, {demoted} demoted, {dropped} dropped "
    "({claim_verify_ms} ms).",
    # Intake telemetry (module="polarius", via the intake. prefix). Operator diagnostics carrying
    # counts and field names only, never free text.
    "intake.clarify.returned": "Intake clarify ({matter_type}): {n_missing} slot(s) still missing.",
    "intake.context.attached": "Intake context attached: {n_facts} known fact(s) seeded into the engine.",
    "intake.conflict": "Intake conflict on {n_conflicts} field(s) (profile vs query).",
    "intake.facts.dropped": "Intake verifier dropped {n_dropped} proposed fact(s).",
    # Per-answer gap backlog telemetry (module="polarius", via the practical. prefix). Counts only,
    # never query or claim text. `practical.gap` is the number of known data gaps surfaced in one
    # answer; `practical.gap_backlog` is the most common missing-data class and how many of the gaps
    # fall into it. The gap_kind values are GAP_KIND_BACKLOG_CODES, kept equal to the engine's.
    "practical.gap": "Answer surfaced {n_gaps} known data gap(s).",
    "practical.gap_backlog": "Gap backlog: {top_gap_kind} leads with {top_count} of {n_gaps} gap(s).",
    # mcp.* (Conduit) events deliberately have no template: they are internal transport telemetry on
    # the system channel, and _render() falls back to the raw code.
}


# In-process pub/sub: maps a channel to its asyncio queues, one per server-sent-event subscriber. A
# channel is either a project_id (that project's log) or SYSTEM_CHANNEL (the whole control room).
_queues: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)

# The control-room channel. Every event with a `module` but no project_id goes here, so the
# operator's /admin/api/events stream shows system, nexus, pythia and polarius events together.
# Project events go to their project's channel instead.
SYSTEM_CHANNEL = "__system__"

# Maps code prefixes to a `module` when emit() is called without one. The first match wins, so more
# specific prefixes come first: `pythia.call.*` is the app's call to the engine (Polarius panel) and
# must match before the bare `pythia.*` engine-telemetry prefix (Pythia panel). An event with no
# project_id whose code matches no prefix is only logged, not sent to any channel.
_MODULE_PREFIXES: list[tuple[str, str]] = [
    ("pythia.call.", "polarius"),
    ("pythia.", "pythia"),
    ("ingest.", "nexus"),
    ("graph-changed", "nexus"),
    ("refresh.", "nexus"),
    ("caselaw.", "nexus"),
    ("embed.", "nexus"),
    ("materialize.", "nexus"),
    ("intelligence", "nexus"),
    ("gnn.", "nexus"),
    ("check.", "nexus"),
    ("analysis.", "polarius"),
    ("retrieval.", "polarius"),
    ("finding.", "polarius"),
    ("killswitch.", "polarius"),
    ("limits.", "polarius"),
    ("budget.", "polarius"),
    # Every new event prefix must be listed here, or its control-room events are silently dropped
    # from the live stream. App-domain events go to polarius; the cost-model alarm goes to system.
    ("compliance.", "polarius"),
    ("handoff.", "polarius"),
    ("export.", "polarius"),
    ("tier.", "polarius"),
    ("aiact.", "polarius"),
    ("finding.", "polarius"),  # AI Act human-review requests
    ("profile.", "polarius"),  # business-profile lifecycle
    ("intake.", "polarius"),  # intake telemetry
    # An engine event, so it must precede the bare `answer.` prefix.
    ("answer.claims.", "pythia"),  # engine claim-verification summary
    ("answer.", "polarius"),  # answer gap telemetry
    ("practical.", "polarius"),  # per-answer gap backlog telemetry
    ("cost.", "system"),
    # Conduit events (only emitted when conduit_enabled) get module="mcp", so they reach the system
    # channel instead of being dropped.
    ("mcp.", "mcp"),
    ("resource.", "system"),
    ("collector.", "system"),
    # Engine-side `nexus.*` codes (self-update, corpus threshold changes) are deliberately not
    # listed; the Nexus poller emits them with an explicit module="nexus".
]


def _module_for_code(code: str) -> str | None:
    for prefix, mod in _MODULE_PREFIXES:
        if code.startswith(prefix):
            return mod
    return None


def _render(code: str, payload: dict[str, Any]) -> str:
    template = FRIENDLY_TEMPLATES.get(code, code)
    try:
        return template.format(**payload)
    except (KeyError, IndexError):
        return template


async def emit(
    code: str,
    *,
    project_id: str | None = None,
    module: Literal["nexus", "pythia", "polarius", "system", "mcp"] | None = None,
    level: Literal["info", "warn", "error"] = "info",
    **payload: Any,
) -> Event:
    """Publish an event to the structlog log and to any subscriber queues.

    A project event (project_id set) goes to that project's channel. A control-room event (no
    project_id but a `module`) goes to SYSTEM_CHANNEL, which feeds the operator's control-room
    stream. `module` defaults to the value derived from `code`.

    Returns the constructed event, for convenience in tests.
    """
    event: Event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "project_id": project_id,
        "module": module if module is not None else _module_for_code(code),
        "level": level,
        "code": code,
        "friendly_message": _render(code, payload),
        "diagnostic_message": f"{code} {payload}",
        "payload": payload,
    }
    log_method = getattr(_log, level, _log.info)
    log_method(code, **event)

    # Fan out to every subscriber on the target channel. A full queue drops the event rather than
    # block: a slow consumer must never stall an analysis or a collector tick.
    channel = project_id if project_id else (SYSTEM_CHANNEL if event["module"] else None)
    if channel:
        for q in list(_queues.get(channel, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                _log.warning("event_queue_full", channel=channel, code=code)
    return event


def subscribe(project_id: str, maxsize: int = 500) -> asyncio.Queue[Event]:
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
    _queues[project_id].append(q)
    return q


def unsubscribe(project_id: str, q: asyncio.Queue[Event]) -> None:
    if q in _queues.get(project_id, []):
        _queues[project_id].remove(q)


async def replay_recent(channel: str, *, limit: int = 50) -> list[Event]:
    """Return up to `limit` recent events for `channel`, read from the tail of the JSON log.

    The event-stream endpoints call this on first connect to show recent history, which also
    survives an app restart. `channel` is either a project_id (rows for that project) or
    SYSTEM_CHANNEL (any row with a `module`, so the control room backfills across subsystems).
    """
    settings = get_settings()
    log_path: Path = settings.log_dir / "app.log"
    if not log_path.exists():
        return []

    system_mode = channel == SYSTEM_CHANNEL
    out: list[Event] = []
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            tail = fh.readlines()[-2000:]  # bounded read
    except OSError:
        return []

    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if system_mode:
            if row.get("module") is None:  # keep any control-room event (system/nexus/pythia/...)
                continue
        elif row.get("project_id") != channel:  # per-project history
            continue
        if "code" not in row or "friendly_message" not in row:
            continue
        out.append(
            {
                "timestamp": row.get("timestamp", ""),
                "project_id": row.get("project_id"),
                "module": row.get("module"),
                "level": row.get("level", "info"),
                "code": row["code"],
                "friendly_message": row["friendly_message"],
                "diagnostic_message": row.get("diagnostic_message", ""),
                "payload": row.get("payload", {}),
            }
        )
        if len(out) >= limit:
            break

    out.reverse()
    return out
