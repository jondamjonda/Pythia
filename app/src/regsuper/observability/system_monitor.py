"""The app-wide system-stats collector.

A single async task, started in the app lifespan, is the only producer of host resource data:

* It is the only caller of `metrics.system_resources()` and `psutil.cpu_percent()` in the
  process. `cpu_percent(interval=None)` measures the time since its previous call, so two
  callers would reset each other's baseline and both get meaningless numbers. Every consumer
  (`admin_system()`, the resource gates, the watchdog) reads `latest()` from the ring instead.
* Cadence is based on a 5-second tick: host vitals, per-core load and MPS every tick, and the
  per-process table less often (it walks every process). Each step is wrapped so a failure
  increments `collector_errors` and emits a `resource.collector.error` event instead of killing
  the loop.
* Threshold crossings emit edge-triggered `resource.*` events (only when the state changes) with
  `module="system"` on the `__system__` channel, so the control room sees pressure changes live.
* Every Nth tick one down-sampled row is written to `system_sample` for history beyond the
  in-memory hour (rows older than 30 days are pruned). If the table is missing, persistence turns
  itself off once instead of flooding the log.

Anything that cannot be measured is reported as unavailable with its age (`as_of`), never as a
made-up 0. Per-process Postgres CPU and memory are invisible from the host under Docker (shown as
one `database` bucket), per-process GPU use needs sudo, and MPS is null whenever no torch process
is alive.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any

import psutil
from sqlalchemy import text

from regsuper.config import get_settings
from regsuper.observability import metrics
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger
from regsuper.storage.database import session_scope

_log = get_logger(__name__)

# One hour of samples at the 5 s tick. start() resizes it from the configured window, so the panel's
# one-hour sparkline is exactly the ring.
_RING: deque[dict[str, Any]] = deque(maxlen=720)
_LATEST_PROCESSES: dict[str, Any] | None = None  # latest per-process memory snapshot
_TASK: asyncio.Task | None = None
_TICK = 0
_COLLECTOR_ERRORS = 0
_PERSIST_ENABLED = True
_PROC_EVERY = 3  # refresh the per-process table every 3rd tick (~15 s); it walks every process
_PRUNE_EVERY = 720  # prune rows older than 30 days about once an hour

# Last emitted level per threshold family, so events fire only when the state changes.
_STATE: dict[str, str] = {"pressure": "normal", "swap": "ok", "disk": "ok"}


def _ring_maxlen() -> int:
    s = get_settings()
    n = int(s.system_ring_seconds / max(s.system_sample_interval_s, 1.0))
    return max(60, min(n, 4096))


def _pressure(vm_pct: float, swap_pct: float) -> str:
    if vm_pct >= 90 or swap_pct >= 50:
        return "high"
    if vm_pct >= 80 or swap_pct >= 20:
        return "elevated"
    return "normal"


def _safe(metric_class: str, fn):
    """Run one collector step, returning an error marker instead of raising.

    On failure, increments the error counter and returns ``("__error__", metric_class, detail)``.
    The loop turns the marker into a `resource.collector.error` event, and the dashboard tile shows
    the metric as unavailable with its age rather than a made-up 0.
    """
    global _COLLECTOR_ERRORS
    try:
        return fn()
    except Exception as exc:
        _COLLECTOR_ERRORS += 1
        _log.warning("collector_step_failed", metric_class=metric_class, error=repr(exc))
        return ("__error__", metric_class, repr(exc))


def _build_vitals() -> dict[str, Any]:
    """Collect host vitals for one tick.

    This is the only place `system_resources()` (and therefore `cpu_percent`) is called.
    """
    now = datetime.now(UTC).isoformat()
    host = _safe("host", metrics.system_resources)
    cores = _safe("per_core", metrics.per_core)
    mps = _safe("mps", metrics.mps_memory)
    errors: list[dict[str, str]] = []
    for v in (host, cores, mps):
        if isinstance(v, tuple) and v and v[0] == "__error__":
            errors.append({"metric_class": v[1], "detail": v[2]})

    host = host if isinstance(host, dict) else None
    vm_pct = host["memory"]["percent"] if host else 0.0
    swap_pct = host["swap"]["percent"] if host else 0.0
    sample: dict[str, Any] = {
        "ts": now,
        "cpu": {
            **(host["cpu"] if host else {}),
            "per_core": cores if isinstance(cores, list) else [],
        },
        "memory": host["memory"] if host else None,
        "swap": host["swap"] if host else None,
        "disk": host["disk"] if host else None,
        "process": host["process"] if host else None,
        "mps": mps if isinstance(mps, dict) else {"available": False, "reason": "collector error"},
        "pressure": _pressure(vm_pct, swap_pct) if host else "unknown",
        "errors": errors,
    }
    return sample


async def _emit_threshold_events(sample: dict[str, Any]) -> None:
    """Emit `resource.*` events, but only when a threshold state changes."""
    mem = sample.get("memory") or {}
    swap = sample.get("swap") or {}
    disk = sample.get("disk") or {}
    vm_pct = float(mem.get("percent") or 0.0)
    swap_pct = float(swap.get("percent") or 0.0)

    pressure = sample["pressure"]
    if pressure != _STATE["pressure"]:
        _STATE["pressure"] = pressure
        if pressure == "high":
            await emit(
                "resource.memory.critical",
                module="system",
                level="error",
                vm_pct=vm_pct,
                swap_pct=swap_pct,
            )
        elif pressure == "elevated":
            await emit(
                "resource.memory.elevated",
                module="system",
                level="warn",
                vm_pct=vm_pct,
                swap_pct=swap_pct,
            )

    swap_state = "active" if swap_pct >= 20 else "ok"
    if swap_state != _STATE["swap"]:
        _STATE["swap"] = swap_state
        if swap_state == "active":
            await emit("resource.swap.active", module="system", level="warn", swap_pct=swap_pct)

    free_gb = float(disk.get("free_bytes") or 0) / 1e9
    disk_state = "low" if (disk and free_gb < 10.0) else "ok"
    if disk_state != _STATE["disk"]:
        _STATE["disk"] = disk_state
        if disk_state == "low":
            await emit(
                "resource.disk.warning",
                module="system",
                level="warn",
                free_gb=free_gb,
                path=str((disk or {}).get("mount", "")),
            )


async def _persist(sample: dict[str, Any]) -> None:
    """Write one down-sampled row to `system_sample`, pruning old rows periodically.

    If the table is missing (or any write fails), persistence turns itself off once rather than
    logging on every tick.
    """
    global _PERSIST_ENABLED
    if not _PERSIST_ENABLED:
        return
    mem = sample.get("memory") or {}
    swap = sample.get("swap") or {}
    disk = sample.get("disk") or {}
    cpu = sample.get("cpu") or {}
    mps = sample.get("mps") or {}
    procs = _LATEST_PROCESSES or {}
    row = {
        "cpu": cpu.get("percent"),
        "mem": mem.get("percent"),
        "swap": swap.get("percent"),
        "disk": disk.get("percent"),
        "uss": (procs.get("project_total") or {}).get("uss_bytes"),
        "mps": mps.get("allocated_bytes") if mps.get("available") else None,
        "pr": sample.get("pressure"),
    }
    try:
        async with session_scope() as s:
            await s.execute(
                text(
                    "INSERT INTO system_sample (cpu_pct,mem_pct,swap_pct,disk_pct,proc_uss_bytes,"
                    "mps_alloc_bytes,pressure) VALUES (:cpu,:mem,:swap,:disk,:uss,:mps,:pr)"
                ),
                row,
            )
            if _TICK % _PRUNE_EVERY == 0:
                await s.execute(
                    text("DELETE FROM system_sample WHERE ts < now() - interval '30 days'")
                )
    except Exception as exc:
        _PERSIST_ENABLED = False
        _log.warning("system_sample_persist_disabled", error=repr(exc))


async def _loop() -> None:
    global _TICK, _LATEST_PROCESSES
    settings = get_settings()
    interval = max(settings.system_sample_interval_s, 1.0)
    persist_every = max(int(settings.system_persist_every), 1)
    # Prime the cpu_percent baselines (aggregate + percpu); the first reading is otherwise 0.0.
    try:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
    except Exception:
        pass
    await asyncio.sleep(1.0)
    while True:
        try:
            _TICK += 1
            sample = _build_vitals()
            # The per-process table walks every process, so refresh it on a slower cadence.
            if _TICK % _PROC_EVERY == 1 or _LATEST_PROCESSES is None:
                seg = _safe("processes", metrics.memory_segmentation)
                if isinstance(seg, dict):
                    _LATEST_PROCESSES = {**seg, "as_of": sample["ts"]}
            _RING.append(sample)
            for e in sample.get("errors", []):
                await emit("resource.collector.error", module="system", level="warn", **e)
            await _emit_threshold_events(sample)
            if _TICK % persist_every == 0:
                await _persist(sample)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("collector_tick_failed", error=repr(exc))
        await asyncio.sleep(interval)


# ── Public accessors (consumers read these and never call psutil themselves) ──────────────────────
def latest() -> dict[str, Any] | None:
    """Return the most recent host-vitals sample, or None before the first one."""
    return _RING[-1] if _RING else None


def latest_processes() -> dict[str, Any] | None:
    return _LATEST_PROCESSES


def ring(window_seconds: int | None = None) -> list[dict[str, Any]]:
    """Return the in-memory ring (up to an hour), optionally trimmed to `window_seconds`."""
    if window_seconds is None:
        return list(_RING)
    keep = max(1, int(window_seconds / max(get_settings().system_sample_interval_s, 1.0)))
    return list(_RING)[-keep:]


def collector_meta() -> dict[str, Any]:
    last = latest()
    age = None
    if last:
        try:
            age = round((datetime.now(UTC) - datetime.fromisoformat(last["ts"])).total_seconds(), 1)
        except Exception:
            age = None
    return {
        "running": _TASK is not None and not _TASK.done(),
        "ticks": _TICK,
        "collector_errors": _COLLECTOR_ERRORS,
        "ring_points": len(_RING),
        "persist_enabled": _PERSIST_ENABLED,
        "as_of_age_s": age,
    }


async def history(
    window_seconds: int, metric_keys: list[str] | None = None, *, max_points: int = 500
) -> dict[str, Any]:
    """Return a trend series for the System panel.

    Windows that fit in the ring are served from memory; longer ones read the down-sampled
    `system_sample` table, thinned to at most `max_points`.
    """
    s = get_settings()
    if window_seconds <= s.system_ring_seconds:
        pts = ring(window_seconds)
        series = [
            {
                "ts": p["ts"],
                "cpu_pct": (p.get("cpu") or {}).get("percent"),
                "mem_pct": (p.get("memory") or {}).get("percent"),
                "swap_pct": (p.get("swap") or {}).get("percent"),
                "disk_pct": (p.get("disk") or {}).get("percent"),
            }
            for p in pts
        ]
        return {"source": "ring", "window_seconds": window_seconds, "points": series}
    # Longer than the ring: read the persisted table and thin it to at most max_points.
    try:
        async with session_scope() as sess:
            rows = (
                (
                    await sess.execute(
                        text(
                            "SELECT ts, cpu_pct, mem_pct, swap_pct, disk_pct FROM system_sample "
                            "WHERE ts >= now() - make_interval(secs => :w) ORDER BY ts ASC"
                        ),
                        {"w": window_seconds},
                    )
                )
                .mappings()
                .all()
            )
    except Exception as exc:
        return {
            "source": "table",
            "error": f"history unavailable ({type(exc).__name__})",
            "points": [],
        }
    rows = list(rows)
    step = max(1, len(rows) // max_points)
    series = [
        {
            "ts": r["ts"].isoformat(),
            "cpu_pct": r["cpu_pct"],
            "mem_pct": r["mem_pct"],
            "swap_pct": r["swap_pct"],
            "disk_pct": r["disk_pct"],
        }
        for r in rows[::step]
    ]
    return {"source": "table", "window_seconds": window_seconds, "points": series}


async def start() -> None:
    """Start the collector task (idempotent). Called from the app lifespan.

    Takes one synchronous sample before the task starts, so `latest()` is available immediately
    and the resource gates and watchdog are active from the first request (the loop itself waits
    a second before its first tick). The CPU figure in this first sample is not meaningful, but the
    memory, swap and disk readings the gates rely on are.
    """
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _RING.clear()
    _RING.__init__(maxlen=_ring_maxlen())  # size to the configured window
    # Prime the cpu_percent baselines, then take one synchronous sample so the gates have real
    # memory, swap and disk readings before any request is served.
    try:
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)
    except Exception:
        pass
    try:
        _RING.append(_build_vitals())
    except Exception as exc:
        _log.warning("system_monitor_initial_sample_failed", error=repr(exc))
    _TASK = asyncio.create_task(_loop(), name="system_monitor")
    _log.info("system_monitor_started", interval_s=get_settings().system_sample_interval_s)


async def stop() -> None:
    global _TASK
    if _TASK is not None:
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):
            pass
        _TASK = None
