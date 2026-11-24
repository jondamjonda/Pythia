"""Hard resource caps without cgroups.

macOS cannot impose OS-level quotas, so the host is protected in three layers:

* Layer A: gates that refuse heavy work before it starts (`gate_storage`, `gate_cpu_mem`,
  `gate_mps`). They raise `ResourceLimitExceeded` with a plain-language reason (served as a 4xx
  or 507) before any destructive write, so the program declines rather than crashing the machine.
* Layer B: a runtime watchdog that reads the system monitor's sample ring (never psutil
  directly, because only one caller may own `cpu_percent`). On a breach sustained for three
  samples (about 15 seconds, so short spikes are ignored) it engages the kill switch. It never
  releases the switch itself; a human does.
* Layer C: the existing kill switch is the master cutoff that both the watchdog and the hard
  weekly spend cap use.

The MPS (Apple GPU) check is advisory: memory can only be measured while a torch process is
alive, so `gate_mps` does nothing when that process's heartbeat is stale.

A cap of 0 turns that ceiling off. By default (`DEFAULT_LIMITS['resource_caps']` in
`limits.py`) the CPU cap (`cpu_refuse_pct`) and the MPS cap (`max_mps_mem_gb`) are 0, so only
the RAM gate (`mem_refuse_pct=95.0`) and the free-disk floor (`min_free_disk_gb=5.0`) are
active. Operators enable the other two through `limits.json` or the admin limits endpoint.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from regsuper.config import get_settings
from regsuper.observability import killswitch, limits, metrics, system_monitor
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger

_log = get_logger(__name__)
_GB = 1024**3


class ResourceLimitExceeded(RuntimeError):
    """Raised by a gate when starting work would breach a hard resource ceiling."""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def _caps() -> dict[str, Any]:
    return limits.get_limits().get("resource_caps") or {}


async def gate_storage(required_bytes: int = 0) -> None:
    """Refuse a write if free disk space would drop below the floor (e.g. filling up mid-embed)."""
    caps = _caps()
    floor_gb = float(caps.get("min_free_disk_gb") or 0.0)
    if floor_gb <= 0:
        return
    try:
        free = shutil.disk_usage(get_settings().data_dir).free
    except OSError:
        return
    if free - required_bytes < floor_gb * _GB:
        reason = (
            f"only {free / _GB:.1f} GB free — below the {floor_gb:.0f} GB floor; "
            f"free up space or lower the storage ceiling"
        )
        await emit(
            "resource.cap.refused", module="system", level="error", kind="storage", reason=reason
        )
        raise ResourceLimitExceeded("resource_cap", reason)


async def gate_cpu_mem() -> None:
    """Refuse new heavy work while RAM or CPU use is over its threshold.

    Reads the system monitor's sample ring, not `system_resources()`, because calling that here
    would reset the monitor's CPU measurement baseline. ``cpu_refuse_pct`` defaults to 0 (off),
    so by default this gate refuses on RAM only.
    """
    caps = _caps()
    sample = system_monitor.latest()
    if not sample:
        return
    mem_pct = float((sample.get("memory") or {}).get("percent") or 0.0)
    cpu_pct = float((sample.get("cpu") or {}).get("percent") or 0.0)
    mem_cap = float(caps.get("mem_refuse_pct") or 0.0)
    cpu_cap = float(caps.get("cpu_refuse_pct") or 0.0)
    if mem_cap > 0 and mem_pct > mem_cap:
        reason = f"memory at {mem_pct:.0f}% (over the {mem_cap:.0f}% ceiling) — try again shortly"
        await emit(
            "resource.cap.refused", module="system", level="warn", kind="memory", reason=reason
        )
        raise ResourceLimitExceeded("resource_cap", reason)
    if cpu_cap > 0 and cpu_pct > cpu_cap:
        reason = f"CPU at {cpu_pct:.0f}% (over the {cpu_cap:.0f}% ceiling) — try again shortly"
        await emit("resource.cap.refused", module="system", level="warn", kind="cpu", reason=reason)
        raise ResourceLimitExceeded("resource_cap", reason)


async def gate_mps() -> None:
    """Refuse a new GNN run if the current MPS allocation already exceeds the ceiling.

    Advisory only: MPS memory is measurable only while a torch process is alive, so with a stale
    heartbeat this does nothing. ``max_mps_mem_gb`` defaults to 0 (off).
    """
    caps = _caps()
    ceil_gb = float(caps.get("max_mps_mem_gb") or 0.0)
    if ceil_gb <= 0:
        return
    mps = metrics.mps_memory()
    if not mps.get("available"):
        return  # no live torch process, so nothing to measure
    alloc = float(mps.get("allocated_bytes") or 0)
    if alloc > ceil_gb * _GB:
        reason = f"MPS allocation {alloc / _GB:.1f} GB over the {ceil_gb:.1f} GB ceiling"
        await emit("resource.cap.refused", module="system", level="warn", kind="mps", reason=reason)
        raise ResourceLimitExceeded("resource_cap", reason)


# ── Layer B: the runtime watchdog ─────────────────────────────────────────────────────────────────
_WATCHDOG_TASK: asyncio.Task | None = None
_BREACH_STREAK = 0
_BREACH_NEEDED = 3  # about 15 s at the 5 s tick; ignores transient spikes


async def _watchdog_loop() -> None:
    global _BREACH_STREAK
    while True:
        try:
            await asyncio.sleep(5)
            caps = _caps()
            if not caps.get("watchdog_enabled", True):
                _BREACH_STREAK = 0
                continue
            sample = system_monitor.latest()
            if not sample:
                continue
            mem_pct = float((sample.get("memory") or {}).get("percent") or 0.0)
            disk = sample.get("disk") or {}
            free_gb = float(disk.get("free_bytes") or 0) / _GB
            mem_cap = float(caps.get("mem_refuse_pct") or 0.0)
            floor_gb = float(caps.get("min_free_disk_gb") or 0.0)

            breached = (mem_cap > 0 and mem_pct >= mem_cap) or (floor_gb > 0 and free_gb < floor_gb)
            if breached:
                _BREACH_STREAK += 1
                if _BREACH_STREAK == 1:
                    await emit(
                        "resource.memory.warn"
                        if mem_cap and mem_pct >= mem_cap
                        else "resource.disk.warning",
                        module="system",
                        level="warn",
                        vm_pct=mem_pct,
                        free_gb=free_gb,
                        path=str(disk.get("mount", "")),
                    )
                if _BREACH_STREAK >= _BREACH_NEEDED and not killswitch.is_killed():
                    metric = "memory" if (mem_cap and mem_pct >= mem_cap) else "disk"
                    killswitch.engage(f"watchdog: {metric} over cap for {_BREACH_STREAK} samples")
                    await emit(
                        "killswitch.engaged",
                        module="polarius",
                        level="error",
                        reason=f"watchdog: sustained {metric} breach",
                    )
            else:
                _BREACH_STREAK = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("watchdog_tick_failed", error=repr(exc))


async def start_watchdog() -> None:
    global _WATCHDOG_TASK
    if _WATCHDOG_TASK is not None and not _WATCHDOG_TASK.done():
        return
    _WATCHDOG_TASK = asyncio.create_task(_watchdog_loop(), name="resource_watchdog")


async def stop_watchdog() -> None:
    global _WATCHDOG_TASK
    if _WATCHDOG_TASK is not None:
        _WATCHDOG_TASK.cancel()
        try:
            await _WATCHDOG_TASK
        except (asyncio.CancelledError, Exception):
            pass
        _WATCHDOG_TASK = None
