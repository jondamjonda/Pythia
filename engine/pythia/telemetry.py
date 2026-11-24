"""In-process telemetry for the Pythia sidecar.

The sidecar is a separate process that shares no event bus or database with the app, so it keeps its
own lightweight, dependency-free telemetry: a bounded ring of per-request traces plus counters,
exposed via GET /metrics (aggregates) and GET /v1/telemetry (raw traces). The app's pythia_monitor
polls these and re-emits them onto its own event bus tagged module="pythia".

The headline metric is GAR, the grounded-answer rate: ok answers over all answer calls, with the
abstain and refuse shares. It comes from the typed PythiaAnswer.verdict, which verify.py cannot mark
"ok" without real citations.
"""

from __future__ import annotations

import contextvars
from collections import deque
from datetime import UTC, datetime
from typing import Any

# The request id shared by all stages of one request (set by the service middleware).
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# Per-request LLM usage sink. The answer() handler sets a fresh list here at request start, before any
# concurrent tasks are created, so asyncio child tasks (which copy the parent context) share the same
# list object and append to it. record_llm_usage() appends to whatever list is set; when the variable is
# None (no answer request in flight, for example /v1/retrieve or a standalone LLM call) nothing is
# appended. The Ollama adapter records no usage at all.
usage_var: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("llm_usage", default=None)

_RING: deque[dict[str, Any]] = deque(maxlen=2000)
_COUNTERS: dict[str, int] = {}
_SEQ = 0


def _bump(key: str, n: int = 1) -> None:
    _COUNTERS[key] = _COUNTERS.get(key, 0) + n


def new_usage_sink() -> list[dict[str, Any]]:
    """Install a fresh per-request usage list on usage_var and return it.

    Called once at the top of answer(), before any asyncio.gather or create_task, so every child
    task's copied context sees the same list object. Context variables copy the binding, not the
    value, so appends made by child tasks are visible to the parent. The caller reads the returned
    list when building audit_trace.
    """
    sink: list[dict[str, Any]] = []
    usage_var.set(sink)
    return sink


def record_llm_usage(stage: str, model: str, tokens_input: int, tokens_output: int, tokens_cached: int = 0) -> None:
    """Record one LLM call's token usage.

    Appends a record to the current request's sink, if one is set (usage_var is None when no answer
    request is in flight), and always bumps the aggregate token and per-stage counters behind
    /metrics. `tokens_cached` is the OpenAI cached-prompt count, which is already included in
    `tokens_input`; the app's pricing subtracts it once, so it must not be subtracted here.
    """
    sink = usage_var.get()
    if sink is not None:
        sink.append(
            {
                "stage": stage,
                "model": model,
                "tokens_input": int(tokens_input),
                "tokens_output": int(tokens_output),
                "tokens_cached": int(tokens_cached),
            }
        )
    # Aggregate counters, recorded whether or not a per-request sink exists.
    _bump("llm_calls_total")
    _bump("llm_tokens_input_total", int(tokens_input))
    _bump("llm_tokens_output_total", int(tokens_output))
    _bump("llm_tokens_cached_total", int(tokens_cached))
    _bump(f"llm_stage_{stage}_calls")
    _bump(f"llm_stage_{stage}_tokens_input", int(tokens_input))
    _bump(f"llm_stage_{stage}_tokens_output", int(tokens_output))


def next_request_id() -> str:
    global _SEQ
    _SEQ += 1
    return f"req-{_SEQ}"


def record(**trace: Any) -> None:
    """Append one trace to the ring and update the counters.

    `kind` is one of request, retrieve, answer, claim_verify or event. Partial traces are fine: the
    middleware records the request envelope and the handlers add answer or retrieve details.
    """
    trace.setdefault("ts", datetime.now(UTC).isoformat())
    _RING.append(trace)
    kind = trace.get("kind")
    if kind == "answer":
        _bump("answer_total")
        verdict = trace.get("verdict") or ("refuse" if trace.get("refused") else "ok")
        _bump(f"answer_{verdict}")
        if trace.get("retried"):
            _bump("answer_retried")
    elif kind == "retrieve":
        _bump("retrieve_total")
        m = trace.get("method") or "cte"
        _bump(f"method_{m}")
        if trace.get("gnn_degraded"):
            _bump("gnn_degraded")
    elif kind == "claim_verify":
        _record_claim_verify(trace)
    if trace.get("http_status", 200) >= 500:
        _bump("http_5xx")


# Claim-loop instruments: counters derived from claim-verify traces, plus per-stage latency.
# They are bumped only by kind="claim_verify" traces, which reasoning.answer.answer() records only when
# claim_verify_enabled is on. Otherwise these keys never appear in _COUNTERS and the "claim_loop" block
# of metrics() reports zeros and None.
#
# The trace carries the claim-ledger summary (claims_total, normative_claims, per_status, per_check;
# the vocabulary is owned by claim_ledger.py and only derived here) plus a per-stage latency map
# {intake_ms, retrieve_ms, generate_ms, verify_ms, claim_verify_ms}. Counters are monotone integer
# tallies bumped per event; latency is kept in a bounded ring per stage for percentiles. Nothing here
# reads config, the database or a clock: the caller supplies the measured milliseconds.
_CLAIM_STAGES: tuple[str, ...] = (
    "intake_ms",
    "retrieve_ms",
    "generate_ms",
    "verify_ms",
    "claim_verify_ms",
)
# Bounded per-stage latency rings for percentiles, separate from the main trace ring so a burst of
# claim-loop events cannot evict answer traces, and vice versa.
_CLAIM_LAT: dict[str, deque[float]] = {st: deque(maxlen=2000) for st in _CLAIM_STAGES}


def _record_claim_verify(trace: dict[str, Any]) -> None:
    """Update the claim-loop counters from a claim_verify trace's ledger summary and latency map.

    Called only from record(). A partial trace is fine: a missing summary or latency map just
    counts the event, never raising KeyError.
    """
    _bump("claim_verify_events")
    summary = trace.get("summary") or {}
    _bump("claim_verify_claims_total", int(summary.get("claims_total", 0) or 0))
    _bump("claim_verify_normative_claims", int(summary.get("normative_claims", 0) or 0))
    # Histogram of final statuses (verified, repaired_verified, demoted_gap, dropped_*, shadow; every
    # claim is "shadow" in shadow mode).
    for status, n in (summary.get("per_status") or {}).items():
        _bump(f"claim_verify_status_{status}", int(n or 0))
    # Per-check pass / fail / skip / advisory tallies (the distribution of verifier outcomes).
    for check, buckets in (summary.get("per_check") or {}).items():
        for st, n in (buckets or {}).items():
            _bump(f"claim_verify_check_{check}_{st}", int(n or 0))
    # Per-stage latency, already measured by the caller. Only the stages supplied are appended.
    for st, ms in (trace.get("latency_ms") or {}).items():
        if st in _CLAIM_LAT and ms is not None:
            _CLAIM_LAT[st].append(float(ms))
    # Repair-loop and budget-breach signals. Only an enforce-mode summary has them (shadow mode runs no
    # repair loop), so they stay 0 otherwise.
    if summary.get("loop_budget_exceeded"):
        _bump("claim_verify_loop_budget_exceeded")
    for key in ("rounds", "repaired", "demoted", "safe_dropped", "llm_repairs"):
        if summary.get(key):
            _bump(f"claim_verify_fixloop_{key}", int(summary.get(key) or 0))


# Per-stage latency of the answer pipeline as a whole (observe only, opt-in). Separate from the
# claim-loop rings above: this is the answer path's own wall-clock time per stage (retrieve, generate,
# verify, ...), recorded via record_stage_latency() from reasoning.answer when
# config.stage_latency_enabled is on. It never enforces a budget or changes a path. Stage names are open
# (any string chosen by the answer path) and appear once recorded. Each stage has a bounded ring that
# feeds the same _percentiles as the claim-loop block.
_STAGE_LAT: dict[str, deque[float]] = {}


def record_stage_latency(stage: str, ms: float) -> None:
    """Append one elapsed-milliseconds sample for an answer-pipeline `stage` to its latency ring.

    Exposed under metrics()['stage_latency_ms'][stage]. It only records a number: no trace or event
    is added and the answer is never touched. A non-numeric or non-finite value is dropped silently.
    """
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return
    if v != v or v in (float("inf"), float("-inf")):  # keep NaN and inf out of the ring
        return
    _STAGE_LAT.setdefault(stage, deque(maxlen=2000)).append(v)


def _stage_latency_metrics() -> dict[str, dict[str, float]]:
    """Return the per-stage answer-latency block for metrics().

    Shape: {stage: {p50, p95, max, last, count}} over each stage's ring, where `last` is the most
    recent sample (a live view of a single turn). Empty when no stage has been recorded. A pure read
    that never raises.
    """
    out: dict[str, dict[str, float]] = {}
    for stage, ring in _STAGE_LAT.items():
        vals = list(ring)
        pct = _percentiles(vals)
        pct["last"] = round(vals[-1], 1) if vals else 0.0
        pct["count"] = len(vals)
        out[stage] = pct
    return out


# Answer-quality tripwires (observe only). A tripwire is a threshold crossed by an answer signal that
# was already computed (for example zero grounded citations, or an empty practical block).
# record_quality_tripwire() bumps a per-tripwire counter and emits an 'answer.quality.tripwire' event
# with the tripwire and the observed value, without changing the answer. It is called only when
# config.quality_tripwires_enabled is on.
def record_quality_tripwire(name: str, value: float | int | bool, threshold: float | int | bool) -> None:
    """Record one fired quality tripwire: bump its counter and emit an 'answer.quality.tripwire' event.

    The app's pythia_monitor forwards events with the 'answer.quality' prefix. This is a monitoring
    signal only: it never raises, demotes or refuses, and any failure inside it is swallowed so it
    cannot harm the answer path.
    """
    try:
        _bump(f"quality_tripwire_{name}")
        _bump("quality_tripwire_events")
        record(
            kind="event",
            event="answer.quality.tripwire",
            module="pythia",
            tripwire=name,
            value=value,
            threshold=threshold,
        )
    except Exception:  # noqa: BLE001 — a tripwire instrument must never break the answer path
        pass


def _quality_tripwire_metrics() -> dict[str, Any]:
    """Return the quality-tripwire block for metrics(): total fired events and a per-tripwire tally.

    A pure read of the counters; zero and empty until a tripwire fires.
    """
    per: dict[str, int] = {
        k[len("quality_tripwire_") :]: v
        for k, v in _COUNTERS.items()
        if k.startswith("quality_tripwire_") and k != "quality_tripwire_events"
    }
    return {"events": _COUNTERS.get("quality_tripwire_events", 0), "per_tripwire": per}


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {"p50": 0.0, "p95": 0.0, "max": 0.0}
    s = sorted(values)

    def pct(p: float) -> float:
        return s[min(len(s) - 1, int(p * len(s)))]

    return {"p50": round(pct(0.50), 1), "p95": round(pct(0.95), 1), "max": round(s[-1], 1)}


def metrics() -> dict[str, Any]:
    """Return aggregate metrics for the Pythia panel.

    Covers the grounded-answer rate and verdict split, the method mix, per-path latency, rates,
    LLM usage, and the optional claim-loop, stage-latency and tripwire blocks.
    """
    answer_total = _COUNTERS.get("answer_total", 0)
    ok = _COUNTERS.get("answer_ok", 0)
    abstain = _COUNTERS.get("answer_abstain", 0)
    refuse = _COUNTERS.get("answer_refuse", 0)
    retrieve_total = _COUNTERS.get("retrieve_total", 0)

    # GAR is null, not 0, when there are no answer calls, so the panel shows it greyed out instead of a
    # misleading 0.
    gar = round(100.0 * ok / answer_total, 1) if answer_total else None
    lat_by_path: dict[str, dict[str, float]] = {}
    for path in {t.get("path") for t in _RING if t.get("total_ms") is not None}:
        vals = [float(t["total_ms"]) for t in _RING if t.get("path") == path and t.get("total_ms") is not None]
        lat_by_path[str(path)] = _percentiles(vals)

    return {
        "answer_total": answer_total,
        "gar_pct": gar,
        "verdict_split": {
            "ok": ok,
            "abstain": abstain,
            "refuse": refuse,
            "ok_pct": round(100.0 * ok / answer_total, 1) if answer_total else None,
            "abstain_pct": round(100.0 * abstain / answer_total, 1) if answer_total else None,
            "refuse_pct": round(100.0 * refuse / answer_total, 1) if answer_total else None,
        },
        "retrieve_total": retrieve_total,
        "method_mix": {m: _COUNTERS.get(f"method_{m}", 0) for m in ("cte", "ppr", "gnn")},
        "gnn_degraded_rate_pct": round(100.0 * _COUNTERS.get("gnn_degraded", 0) / retrieve_total, 1)
        if retrieve_total
        else 0.0,
        "self_repair_rate_pct": round(100.0 * _COUNTERS.get("answer_retried", 0) / answer_total, 1)
        if answer_total
        else 0.0,
        "http_5xx": _COUNTERS.get("http_5xx", 0),
        "latency_ms": lat_by_path,
        "ring_size": len(_RING),
        # LLM cost-observability counters (all 0 with the Ollama provider, which records no usage).
        # Per-stage token totals let the panel attribute LLM spend to each class of call.
        "llm_usage": {
            "calls_total": _COUNTERS.get("llm_calls_total", 0),
            "tokens_input_total": _COUNTERS.get("llm_tokens_input_total", 0),
            "tokens_output_total": _COUNTERS.get("llm_tokens_output_total", 0),
            "tokens_cached_total": _COUNTERS.get("llm_tokens_cached_total", 0),
            "by_stage": {
                stage: {
                    "calls": _COUNTERS.get(f"llm_stage_{stage}_calls", 0),
                    "tokens_input": _COUNTERS.get(f"llm_stage_{stage}_tokens_input", 0),
                    "tokens_output": _COUNTERS.get(f"llm_stage_{stage}_tokens_output", 0),
                }
                # The known stages, including the claim-verify and repair lanes, so their spend shows up
                # in by_stage. A stage appears only once it has at least one call.
                for stage in (
                    "decompose",
                    "generate",
                    "generate_retry",
                    "verify_faithfulness",
                    "metagate",
                    "atomize_assist",
                    "claim_entailment",
                    "claim_repair_redraft",
                )
                if _COUNTERS.get(f"llm_stage_{stage}_calls", 0)
            },
        },
        # Claim-loop instruments: zero or None until a claim_verify trace is recorded (only when
        # claim_verify_enabled is on). The panel shows cost (per-stage latency) next to quality (claim
        # status and check distributions), the evidence needed before switching the loop to enforce.
        "claim_loop": _claim_loop_metrics(),
        # Per-stage latency of the whole answer pipeline (observe only), separate from
        # claim_loop.stage_latency_ms, which covers only the claim-verify stages. Recorded only when
        # config.stage_latency_enabled is on; otherwise {}.
        "stage_latency_ms": _stage_latency_metrics(),
        # Quality tripwire counters (observe only): all zero until a tripwire fires, which happens only
        # when config.quality_tripwires_enabled is on.
        "quality_tripwires": _quality_tripwire_metrics(),
    }


def _claim_loop_metrics() -> dict[str, Any]:
    """Return the claim-loop block for metrics().

    Every field is zero or empty until the loop has run at least once (it records a trace only when
    claim_verify_enabled is on): events is 0, the distributions are empty and the latency
    percentiles are 0.0. A pure read of the counters and rings that never raises.
    """
    events = _COUNTERS.get("claim_verify_events", 0)
    claims_total = _COUNTERS.get("claim_verify_claims_total", 0)
    # Histogram over the final statuses actually seen.
    per_status = {
        k[len("claim_verify_status_") :]: v for k, v in _COUNTERS.items() if k.startswith("claim_verify_status_")
    }
    # Per-check pass / fail / skip / advisory, rebuilt from the flat "claim_verify_check_<check>_<status>" keys.
    per_check: dict[str, dict[str, int]] = {}
    for k, v in _COUNTERS.items():
        if not k.startswith("claim_verify_check_"):
            continue
        rest = k[len("claim_verify_check_") :]
        check, _, status = rest.rpartition("_")
        if check:
            per_check.setdefault(check, {})[status] = v
    # Per-check pass rate, pass / (pass + fail); None when a check never reached pass or fail (all skipped).
    check_pass_rate: dict[str, float | None] = {}
    for check, buckets in per_check.items():
        decided = buckets.get("pass", 0) + buckets.get("fail", 0)
        check_pass_rate[check] = round(100.0 * buckets.get("pass", 0) / decided, 1) if decided else None
    return {
        "events": events,
        "claims_total": claims_total,
        "normative_claims": _COUNTERS.get("claim_verify_normative_claims", 0),
        "claims_per_event": round(claims_total / events, 2) if events else None,
        "per_status": per_status,
        "per_check": per_check,
        "check_pass_rate_pct": check_pass_rate,
        # Per-stage latency percentiles (cost visibility). Empty rings give 0.0 via _percentiles, so the
        # block is well-shaped even before the loop first runs.
        "stage_latency_ms": {st: _percentiles(list(_CLAIM_LAT[st])) for st in _CLAIM_STAGES},
        # Repair-loop histogram and LLM-budget breach count (enforce mode only; 0 in shadow mode).
        "loop_budget_exceeded": _COUNTERS.get("claim_verify_loop_budget_exceeded", 0),
        "fix_loop": {
            "rounds": _COUNTERS.get("claim_verify_fixloop_rounds", 0),
            "repaired": _COUNTERS.get("claim_verify_fixloop_repaired", 0),
            "demoted": _COUNTERS.get("claim_verify_fixloop_demoted", 0),
            "safe_dropped": _COUNTERS.get("claim_verify_fixloop_safe_dropped", 0),
            "llm_repairs": _COUNTERS.get("claim_verify_fixloop_llm_repairs", 0),
        },
    }


def traces(limit: int = 50) -> list[dict[str, Any]]:
    return list(_RING)[-limit:][::-1]  # newest first
