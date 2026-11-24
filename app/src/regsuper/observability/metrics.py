"""Live system resource metrics and storage breakdowns.

Backs the dashboard's CPU, RAM and disk gauges and the storage breakdown table, using psutil
only (no external services). Directory walks are depth-capped so they stay under a second even
when the corpus holds thousands of XML and PDF files.
"""

from __future__ import annotations

import os
import shutil
import time as _time
from pathlib import Path
from typing import Any

import psutil

from regsuper.config import get_settings


def system_resources() -> dict[str, Any]:
    """Snapshot CPU, RAM, swap and disk usage for the dashboard gauges.

    Only the system monitor's collector should call this (see `system_monitor`); other code reads
    its latest sample.
    """
    # `cpu_percent(interval=None)` is non-blocking and measures the time since the previous call.
    # The first call after process start may return 0.0, which is acceptable for a polling
    # dashboard; later calls are accurate.
    cpu = psutil.cpu_percent(interval=None)
    cpu_count = psutil.cpu_count(logical=True) or 1
    load = None
    try:
        # psutil.getloadavg works on Windows too (1-min / 5-min / 15-min).
        load = psutil.getloadavg()
    except (AttributeError, OSError):
        load = (0.0, 0.0, 0.0)

    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()

    settings = get_settings()
    data_root = settings.data_dir.resolve()
    # Disk usage on the partition that hosts data_dir.
    try:
        disk = shutil.disk_usage(data_root)
        disk_total = disk.total
        disk_used = disk.used
        disk_free = disk.free
    except OSError:
        disk_total = disk_used = disk_free = 0

    # Process self stats — useful to verify the API isn't leaking memory.
    proc = psutil.Process()
    with proc.oneshot():
        rss = proc.memory_info().rss
        proc_cpu = proc.cpu_percent(interval=None)
        threads = proc.num_threads()
        try:
            open_files = len(proc.open_files())
        except (psutil.AccessDenied, OSError):
            open_files = -1

    return {
        "cpu": {
            "percent": round(cpu, 1),
            "count": cpu_count,
            "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2),
            "load_15m": round(load[2], 2),
        },
        "memory": {
            "total_bytes": vm.total,
            "used_bytes": vm.used,
            "available_bytes": vm.available,
            "percent": vm.percent,
        },
        "swap": {
            "total_bytes": sm.total,
            "used_bytes": sm.used,
            "percent": sm.percent,
        },
        "disk": {
            "mount": str(data_root.anchor or data_root),
            "total_bytes": disk_total,
            "used_bytes": disk_used,
            "free_bytes": disk_free,
            "percent": round(100 * disk_used / disk_total, 1) if disk_total else 0.0,
        },
        "process": {
            "pid": proc.pid,
            "rss_bytes": rss,
            "cpu_percent": round(proc_cpu, 1),
            "threads": threads,
            "open_files": open_files,
        },
    }


def storage_breakdown(
    root: Path | None = None,
    *,
    max_depth: int = 2,
    top_n: int = 50,
) -> dict[str, Any]:
    """Walk `data_dir` and `log_dir` and return the size of each directory.

    Depth-capped so the walk stays cheap on large corpora; anything deeper than `max_depth` is
    rolled up into its deepest allowed ancestor.
    """
    settings = get_settings()
    roots: list[Path]
    if root is None:
        roots = [settings.data_dir.resolve(), settings.log_dir.resolve()]
    else:
        roots = [root.resolve()]

    entries: list[dict[str, Any]] = []
    grand_total = 0

    for r in roots:
        if not r.exists():
            continue
        for path, depth, size, n_files in _walk_with_sizes(r, max_depth=max_depth):
            entries.append(
                {
                    "path": str(path),
                    "rel_path": str(path.relative_to(r)) if path != r else ".",
                    "root": str(r),
                    "depth": depth,
                    "size_bytes": size,
                    "n_files": n_files,
                }
            )
        # Each root walked separately; only top-level dir contributes to grand total.
        try:
            for child in r.iterdir():
                if child.is_file():
                    grand_total += child.stat().st_size
                elif child.is_dir():
                    grand_total += _dir_total(child)
        except OSError:
            pass

    entries.sort(key=lambda e: e["size_bytes"], reverse=True)
    return {
        "roots": [str(r) for r in roots],
        "grand_total_bytes": grand_total,
        "entries": entries[:top_n],
        "truncated": len(entries) > top_n,
    }


def _dir_total(path: Path) -> int:
    """Sum the size of every file under `path`, recursively, skipping unreadable entries."""
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                try:
                    total += child.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _walk_with_sizes(
    root: Path,
    *,
    max_depth: int,
) -> list[tuple[Path, int, int, int]]:
    """Return (path, depth, total_bytes, n_files) for `root` and each subdirectory to max_depth.

    Subtrees deeper than max_depth are rolled up into their deepest allowed ancestor. Returns a
    list rather than a generator so callers can sort it.
    """
    rows: list[tuple[Path, int, int, int]] = []

    def visit(path: Path, depth: int) -> tuple[int, int]:
        my_files = 0
        my_size = 0
        try:
            children = list(path.iterdir())
        except OSError:
            return 0, 0

        for child in children:
            try:
                if child.is_file():
                    try:
                        sz = child.stat().st_size
                    except OSError:
                        sz = 0
                    my_size += sz
                    my_files += 1
                elif child.is_dir():
                    if depth < max_depth:
                        sub_size, sub_files = visit(child, depth + 1)
                        my_size += sub_size
                        my_files += sub_files
                    else:
                        sub_size = _dir_total(child)
                        sub_files = sum(1 for _ in child.rglob("*") if _.is_file())
                        my_size += sub_size
                        my_files += sub_files
            except OSError:
                continue

        rows.append((path, depth, my_size, my_files))
        return my_size, my_files

    visit(root, 0)
    return rows


def _repo_root() -> Path:
    """Return the app root (the directory holding pyproject.toml), else data_dir's parent."""
    try:
        root = Path(__file__).resolve().parents[3]
        if (root / "pyproject.toml").exists():
            return root
    except Exception:
        pass
    return get_settings().data_dir.resolve().parent


_MEM_GROUP_LABELS = {
    "backend": "Backend API (this monitor)",
    "database": "Database — Docker VM",
    "frontend": "Frontend — Node/Next",
    "calibration": "Calibration — Streamlit",
    # Native Pythia engine processes
    "sidecar": "Pythia sidecar — uvicorn",
    "gnn_job": "Nexus GNN training",
    "ollama": "Ollama — local LLM",
}


def _classify_proc(name: str, cmd: str, pid: int, self_pid: int) -> str | None:
    n = name.lower()
    c = cmd.lower()
    if pid == self_pid or "regsuper.api" in c or ("uvicorn" in c and "regsuper" in c):
        return "backend"
    if "streamlit" in c or "calibration_app" in c:
        return "calibration"
    if n == "next-server" or ("next" in c and ("node" in n or "next" in n or "npm" in c)):
        return "frontend"
    # Native Pythia engine processes run outside Docker, so unlike Postgres they are visible from
    # the host. Order matters: they are python/uvicorn processes too, so they are checked after the
    # app backend (which requires "regsuper" in the command line, so the two cannot collide).
    if "pythia.service" in c or ("uvicorn" in c and "pythia" in c):
        return "sidecar"
    if "pretrain_stage" in c or "train_stage" in c or ("pythia" in c and "train" in c):
        return "gnn_job"
    if "ollama" in n or "ollama" in c:
        return "ollama"
    if (
        "docker" in n
        or "com.docker" in c
        or n in ("vpnkit", "qemu", "qemu-system-aarch64")
        or "virtiofs" in n
    ):
        return "database"
    return None


def per_core() -> list[float]:
    """Return per-core CPU percentages. Only the system monitor's collector may call this.

    psutil tracks the per-core baseline separately from the aggregate, but both measure the time
    since the previous call, so an on-demand caller would corrupt the collector's readings.
    """
    try:
        return [round(x, 1) for x in psutil.cpu_percent(interval=None, percpu=True)]
    except Exception:
        return []


def mps_memory() -> dict[str, Any]:
    """Return MPS (Apple GPU) memory allocation from the torch heartbeat file.

    The app's environment has no torch, so the torch process (GNN training, or the engine with
    method='gnn') writes `data/gpu_mps.json` and this only reads it. MPS is measurable only while a
    torch process is alive: a heartbeat older than 30 seconds means none is running, so the result
    is `available=False` with an "idle" reason rather than a stale or made-up number. MPS caps are
    therefore advisory and only enforced while the heartbeat is fresh.
    """
    f = get_settings().data_dir / "gpu_mps.json"
    if not f.exists():
        return {"available": False, "reason": "no GNN/torch process has run (no heartbeat file)"}
    try:
        import json as _json

        d = _json.loads(f.read_text(encoding="utf-8"))
        age = max(0.0, _time.time() - float(d.get("ts", 0)))
        stale = age > 30.0
        return {
            "available": not stale,
            "stale": stale,
            "age_s": round(age, 1),
            "allocated_bytes": d.get("allocated_bytes"),
            "driver_allocated_bytes": d.get("driver_allocated_bytes"),
            "device": d.get("device"),
            "reason": "MPS idle (no torch process)" if stale else None,
        }
    except (OSError, ValueError, TypeError):
        return {"available": False, "reason": "heartbeat unreadable"}


def memory_segmentation() -> dict[str, Any]:
    """Return current memory use broken down by project component.

    USS (unique set size) is memory private to a process, so summing it across processes does not
    double-count shared libraries; it is the accurate figure for what the project occupies. RSS
    includes shared pages and overcounts when summed. Docker is reported as the whole Docker
    Desktop VM (Postgres plus VM overhead), because per-container memory is not visible from the
    host.
    """
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    self_pid = os.getpid()
    groups: dict[str, dict[str, Any]] = {}
    track_uss = track_rss = 0

    for p in psutil.process_iter(["pid", "name"]):
        try:
            pid = p.info["pid"]
            name = p.info.get("name") or ""
            nl = name.lower()
            if not (
                pid == self_pid
                or any(
                    t in nl
                    for t in (
                        "python",
                        "node",
                        "docker",
                        "streamlit",
                        "next",
                        "vpnkit",
                        "qemu",
                        "ollama",
                    )
                )
            ):
                continue
            try:
                cmd = " ".join(p.cmdline())
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                cmd = name
            grp = _classify_proc(name, cmd, pid, self_pid)
            if grp is None:
                continue
            try:
                rss = p.memory_info().rss
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            uss, exact = rss, False
            try:
                uss = p.memory_full_info().uss
                exact = True
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                pass
            b = groups.setdefault(
                grp,
                {
                    "key": grp,
                    "label": _MEM_GROUP_LABELS[grp],
                    "rss_bytes": 0,
                    "uss_bytes": 0,
                    "n_procs": 0,
                    "uss_exact": True,
                },
            )
            b["rss_bytes"] += rss
            b["uss_bytes"] += uss
            b["n_procs"] += 1
            if not exact:
                b["uss_exact"] = False
            if pid == self_pid:
                track_uss, track_rss = uss, rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    group_list = sorted(groups.values(), key=lambda g: g["uss_bytes"], reverse=True)
    proj_uss = sum(g["uss_bytes"] for g in group_list)
    proj_rss = sum(g["rss_bytes"] for g in group_list)
    for g in group_list:
        g["percent_of_ram"] = round(100 * g["uss_bytes"] / vm.total, 1) if vm.total else 0.0

    if vm.percent >= 90 or sm.percent >= 50:
        pressure = "high"
    elif vm.percent >= 80 or sm.percent >= 20:
        pressure = "elevated"
    else:
        pressure = "normal"

    return {
        "system": {
            "total_bytes": vm.total,
            "used_bytes": vm.used,
            "available_bytes": vm.available,
            "percent": vm.percent,
        },
        "swap": {"total_bytes": sm.total, "used_bytes": sm.used, "percent": sm.percent},
        "pressure": pressure,
        "groups": group_list,
        "project_total": {
            "uss_bytes": proj_uss,
            "rss_bytes": proj_rss,
            "percent_of_ram": round(100 * proj_uss / vm.total, 1) if vm.total else 0.0,
        },
        "tracking": {"pid": self_pid, "uss_bytes": track_uss, "rss_bytes": track_rss},
        "metric_note": (
            "USS = private memory per process (summed without double-counting shared "
            "libraries — the accurate project footprint). RSS includes shared pages. "
            "Docker = whole Docker Desktop VM (Postgres + VM overhead)."
        ),
    }


_STORAGE_SEG_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_STORAGE_SEG_TTL = 45.0


def _path_size(p: Path) -> tuple[int, int]:
    """Return (total_bytes, n_files) for a file or directory tree, skipping unreadable entries."""
    if p.is_file():
        try:
            return p.stat().st_size, 1
        except OSError:
            return 0, 1
    total = n = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
                n += 1
        except OSError:
            continue
    return total, n


def project_storage_segmentation(*, force: bool = False) -> dict[str, Any]:
    """Return whole-project disk usage, grouped into buckets.

    Cached for about 45 seconds because walking node_modules, .venv and .git is expensive. The
    database size is added by the caller.
    """
    now = _time.time()
    cached = _STORAGE_SEG_CACHE["data"]
    if not force and cached is not None and now - _STORAGE_SEG_CACHE["ts"] < _STORAGE_SEG_TTL:
        return cached

    root = _repo_root()
    buckets: list[dict[str, Any]] = []

    def add(key: str, label: str, rel: str, note: str = "") -> None:
        p = root / rel
        if not p.exists():
            return
        size, n = _path_size(p)
        buckets.append({"key": key, "label": label, "size_bytes": size, "n_files": n, "note": note})

    add("python_venv", "Python venv (.venv)", ".venv", "backend dependencies")
    add("node_modules", "Frontend deps (node_modules)", "frontend/node_modules", "")
    add("git", "Git history (.git)", ".git", "")
    add("db_dump", "DB snapshot (db_dump)", "db_dump", "Git-LFS Postgres dump")
    add("data", "Ingested data (data/)", "data", "")
    add("logs", "Logs (logs/)", "logs", "")
    add("frontend_build", "Next build cache (.next)", "frontend/.next", "")

    code_dirs = [
        "src",
        "apps",
        "scripts",
        "migrations",
        "docs",
        "amendments",
        "deploy",
        "docker",
        "frontend/src",
        "frontend/public",
    ]
    code_b = code_n = 0
    for d in code_dirs:
        p = root / d
        if p.exists():
            s, n = _path_size(p)
            code_b += s
            code_n += n
    buckets.append(
        {
            "key": "source_code",
            "label": "Source code",
            "size_bytes": code_b,
            "n_files": code_n,
            "note": "src, frontend/src, apps, scripts, migrations, docs…",
        }
    )

    dev_b = dev_n = 0
    for f in ["Regulatory_Super_App_Dev_Plan.pdf", "plan_v3.txt", "_plan.txt"]:
        p = root / f
        if p.exists():
            s, n = _path_size(p)
            dev_b += s
            dev_n += n
    if dev_b:
        buckets.append(
            {
                "key": "dev_docs",
                "label": "Dev plan & docs",
                "size_bytes": dev_b,
                "n_files": dev_n,
                "note": "PDF + plan_v3.txt + _plan.txt",
            }
        )

    out = {
        "root": str(root),
        "buckets": buckets,
        "fs_total_bytes": sum(b["size_bytes"] for b in buckets),
    }
    _STORAGE_SEG_CACHE["ts"] = now
    _STORAGE_SEG_CACHE["data"] = out
    return out


def storage_health(*, limit_bytes: int | None = None) -> dict[str, Any]:
    """Tiny helper used by the limits module — total bytes under data_dir."""
    settings = get_settings()
    used = _dir_total(settings.data_dir)
    return {
        "used_bytes": used,
        "limit_bytes": limit_bytes,
        "over_limit": (limit_bytes is not None and used > limit_bytes),
    }
