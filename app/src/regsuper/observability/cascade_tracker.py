"""Recent analysis-cascade runs, for the control room's cascade board.

An in-memory ring of run-level records (started, which layers ran, completed or failed, and
counts), aggregated across projects. Detailed per-layer progress for a single project stays on
that project's server-sent event log; the two are deliberately separate channels.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any

_RUNS: deque[dict[str, Any]] = deque(maxlen=50)
_SEQ = 0


def start_run(
    project_id: Any, layers: list[str], *, law_source: str, force: bool
) -> dict[str, Any]:
    global _SEQ
    _SEQ += 1
    run: dict[str, Any] = {
        "run_id": f"run-{_SEQ}",
        "project_id": str(project_id),
        "layers": list(layers),
        "law_source": law_source,
        "force": force,
        "started": datetime.now(UTC).isoformat(),
        "ended": None,
        "status": "running",
        "counts": {},
        "failed_layer": None,
        "error": None,
    }
    _RUNS.append(run)
    return run


def finish_run(run: dict[str, Any], counts: dict[str, Any]) -> None:
    run["status"] = "complete"
    run["ended"] = datetime.now(UTC).isoformat()
    run["counts"] = counts


def fail_run(run: dict[str, Any], layer: str | None, error: str) -> None:
    run["status"] = "failed"
    run["ended"] = datetime.now(UTC).isoformat()
    run["failed_layer"] = layer
    run["error"] = (error or "")[:300]


def recent(limit: int = 30) -> list[dict[str, Any]]:
    return list(_RUNS)[-limit:][::-1]  # newest first


def active_count() -> int:
    return sum(1 for r in _RUNS if r["status"] == "running")
