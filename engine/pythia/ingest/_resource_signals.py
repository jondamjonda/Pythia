"""Cross-process control signals for the engine's long-running torch loops (embedding, GNN training).

The web app's environment has no torch, so it can neither measure Apple MPS (GPU) memory nor stop a
torch loop in-process. The two processes coordinate through files in the shared `data/` directory
(`PYTHIA_DATA_DIR`): an MPS heartbeat, a kill switch, a free-disk floor, the auto-ingest switch and
run lock, and a RAM check. Everything here is best-effort and import-light: a missing, garbled or
unreadable file must never crash a run; it only means a signal is unavailable, which is reported.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from pythia.config import get_settings

_log = logging.getLogger("pythia.ingest._resource_signals")

KILL_FILENAME = "KILL_SWITCH"
MPS_HEARTBEAT_FILENAME = "gpu_mps.json"
# On/off mirror for automatic ingestion. The app is the sole writer: on every enable or disable it
# writes "1" (on) or "0" or deletes the file (off), next to its authoritative settings row. The
# engine only reads it, at the start of every cycle, and never writes it, so the two cannot disagree
# about who owns the setting. A missing file means off.
AUTO_INGEST_ENABLED_FILENAME = "AUTO_INGEST_ENABLED"
# Single-flight run lock for the auto-ingest cycle. Holds the PID and started_at so a crashed run's
# lock can be reclaimed once it is older than lock_ttl_s. The scheduler's max_instances=1 guards
# within one process; this file guards across processes, so a cron run and a manual run never overlap.
AUTO_INGEST_LOCK_FILENAME = "AUTO_INGEST.lock"
# Default free-disk floor in GB, matching the app's
# regsuper.limits.DEFAULT_LIMITS["resource_caps"]["min_free_disk_gb"].
# Override with PYTHIA_MIN_FREE_DISK_GB; 0 disables the engine-side disk check.
_DEFAULT_MIN_FREE_DISK_GB = 5.0
_GB = 1024**3


class KillSwitchEngaged(RuntimeError):
    """Raised between batches when the KILL_SWITCH flag file is present."""


class FreeDiskExhausted(RuntimeError):
    """Raised between batches when free disk has dropped below the floor."""


def _data_dir() -> Path:
    return Path(get_settings().data_dir)


def _min_free_disk_gb() -> float:
    try:
        return float(os.environ.get("PYTHIA_MIN_FREE_DISK_GB", _DEFAULT_MIN_FREE_DISK_GB))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_FREE_DISK_GB


def write_mps_heartbeat(device: str) -> None:
    """Write `gpu_mps.json` so the app can see the live MPS allocation.

    Called every batch with the live allocation and a wall-clock `ts`. The app's system panel and its
    MPS gate read the file; a heartbeat older than 30 s means no torch process is alive (MPS is only
    measurable while torch runs, so the app never reports a fake 0). Silently does nothing when not
    on MPS or when the shared data directory is not writable: the heartbeat is advisory and must
    never affect the run.
    """
    if device != "mps":
        return
    try:
        import torch

        allocated = int(torch.mps.current_allocated_memory())
        driver = int(torch.mps.driver_allocated_memory())
    except Exception:  # noqa: BLE001 - torch lacks the API or not on MPS: skip the heartbeat
        return
    payload = {
        "ts": time.time(),
        "device": "mps",
        "allocated_bytes": allocated,
        "driver_allocated_bytes": driver,
    }
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (MPS_HEARTBEAT_FILENAME + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(d / MPS_HEARTBEAT_FILENAME)  # atomic swap so a reader never sees a half-written file
    except OSError:
        return


def check_between_batches() -> None:
    """Poll the shared control signals; call at the start of each batch, before encoding or writing.

    The kill switch is a flag file created by the app (or by `touch data/KILL_SWITCH` in a shell);
    polling it between batches lets the loop abort cleanly, so one click halts both processes. The
    free-disk floor matches the app's `min_free_disk_gb` limit, so a long run cannot fill the disk.

    Raises:
        KillSwitchEngaged: the kill switch flag file is present.
        FreeDiskExhausted: free disk has fallen below the floor.
    """
    kill = _data_dir() / KILL_FILENAME
    try:
        killed = kill.exists()
    except OSError:
        killed = False
    if killed:
        reason = ""
        try:
            reason = kill.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        raise KillSwitchEngaged(f"KILL_SWITCH present ({reason or 'no reason given'}) — aborting embed run")

    floor_gb = _min_free_disk_gb()
    if floor_gb > 0:
        try:
            free = shutil.disk_usage(_data_dir()).free
        except OSError:
            return  # cannot measure: do not block the run on a stat failure
        if free < floor_gb * _GB:
            raise FreeDiskExhausted(
                f"only {free / _GB:.1f} GB free — below the {floor_gb:.0f} GB floor; aborting embed run"
            )


# --------------------------------------------------------------------------- auto-ingest on/off mirror
def auto_ingest_enabled_file() -> bool:
    """Read the app-written auto-ingest switch (data/AUTO_INGEST_ENABLED).

    True only if the file exists and its first non-whitespace character is '1'. A missing, "0",
    unreadable or garbled file reads as off. The engine never writes this file; the app's database
    settings row is authoritative and this file mirrors it.
    """
    f = _data_dir() / AUTO_INGEST_ENABLED_FILENAME
    try:
        if not f.exists():
            return False
        return f.read_text(encoding="utf-8").strip().startswith("1")
    except OSError:
        return False


# --------------------------------------------------------------------------- memory pre-check gate
def memory_percent() -> float | None:
    """Return current system RAM utilisation in percent, or None if it cannot be measured.

    Sources, in order (psutil is optional in the engine environment):

      1. psutil.virtual_memory().percent, when psutil is importable (precise and live).
      2. A system snapshot the app may write to data/ (`system.json` or `sysmem.json`) with a
         `mem_percent`, `memory_percent` or `ram_percent` field. This reuses the app's measurement
         without adding a psutil dependency to the engine.

    When neither is available the caller fails open: a signal that cannot be read is never a reason
    to block the cycle (the same rule as the free-disk check).
    """
    try:  # 1. precise and in-process, if psutil is installed
        import psutil  # noqa: PLC0415 - optional dependency; often absent

        return float(psutil.virtual_memory().percent)
    except Exception:  # noqa: BLE001 - psutil missing or changed: fall back to the snapshot
        pass
    d = _data_dir()
    for name in ("system.json", "sysmem.json"):
        f = d / name
        try:
            if not f.exists():
                continue
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("mem_percent", "memory_percent", "ram_percent"):
            val = doc.get(key)
            if isinstance(val, (int, float)):
                return float(val)
    return None


def memory_ok(ceiling_pct: float) -> bool:
    """Return True if there is enough free RAM to run the heavy embed and reindex stages this cycle.

    True when system RAM use is strictly below `ceiling_pct` (default 80, below the roughly 90% point
    where swapping starts), or when it cannot be measured (fail open; see memory_percent). At or above
    the ceiling the orchestrator skips embedding and reindexing; the newly ingested acts stay
    unembedded until the next cycle. The corpus remains consistent meanwhile, because graph and
    full-text retrieval do not need the embeddings.
    """
    pct = memory_percent()
    if pct is None:
        return True  # unmeasurable: a missing signal is not a reason to halt
    return pct < ceiling_pct


# --------------------------------------------------------------------------- single-flight run lock
def _lock_path() -> Path:
    return _data_dir() / AUTO_INGEST_LOCK_FILENAME


def _read_lock() -> dict | None:
    """Return the lock file's {pid, started_at} payload, or None if it is absent or garbled."""
    try:
        return json.loads(_lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_running(*, lock_ttl_s: int) -> bool:
    """Return True if a fresh run lock is held, i.e. a cycle is in flight.

    A lock older than lock_ttl_s is stale (left by a crashed run) and reported as not running, so a
    dead lock can never block the pipeline for good. An unreadable or garbled lock also counts as not
    running; acquire_lock will overwrite it.
    """
    doc = _read_lock()
    if not doc:
        return False
    try:
        started = float(doc.get("started_at", 0.0))
    except (TypeError, ValueError):
        return False
    if lock_ttl_s > 0 and (time.time() - started) >= lock_ttl_s:
        return False  # stale, so reclaimable
    return True


def acquire_lock(*, lock_ttl_s: int) -> bool:
    """Try to take the single-flight run lock.

    Returns True if acquired, or False if a fresh lock already exists (another cycle is in flight, so
    the caller must refuse with 'already_running'). A stale lock (older than lock_ttl_s) is reclaimed.
    The write is atomic (temp file plus replace), so a reader never sees a half-written lock.

    Check-then-write is not atomic at the OS level, so a tiny race window remains. The scheduler's
    max_instances=1 is the primary in-process guard and this file covers the cross-process case;
    together they make two concurrent embedding runs practically impossible.
    """
    if is_running(lock_ttl_s=lock_ttl_s):
        return False
    payload = {"pid": os.getpid(), "started_at": time.time()}
    try:
        d = _data_dir()
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (AUTO_INGEST_LOCK_FILENAME + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(_lock_path())  # atomic swap
        return True
    except OSError as exc:
        _log.warning("auto-ingest: could not write run lock: %r", exc)
        return False


def release_lock() -> None:
    """Release the single-flight run lock (idempotent; a missing lock is fine).

    A removal failure is logged, not raised: the TTL lets the next cycle reclaim the lock anyway.
    """
    try:
        _lock_path().unlink(missing_ok=True)
    except OSError as exc:  # noqa: BLE001
        _log.warning("auto-ingest: could not release run lock: %r", exc)
