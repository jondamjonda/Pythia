"""Bridge the Pythia engine's health to the app's event bus.

The engine runs as a separate process with its own telemetry. This poller fetches its `/health`
periodically and emits `module="pythia"` events on the `__system__` channel when the state
changes (engine unreachable, Ollama up or down), so the control room's Pythia log and alarm bar
show live status. The panel's gauges read the engine's `/metrics` directly (through
`/admin/api/pythia/metrics`); this module only bridges events and alarms.
"""

from __future__ import annotations

import asyncio

from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger
from regsuper.pythia import client as pythia_client

_log = get_logger(__name__)
_TASK: asyncio.Task | None = None
_STATE: dict[str, object] = {"reachable": None, "ollama": None}


async def _poll_once() -> None:
    s = get_settings()
    if not (s.pythia_enabled and s.pythia_url):
        return
    try:
        h = await pythia_client.health()
    except Exception as exc:
        if _STATE["reachable"] is not False:
            await emit(
                "pythia.health.unreachable",
                module="pythia",
                level="error",
                detail=type(exc).__name__,
            )
        _STATE["reachable"] = False
        _STATE["ollama"] = None
        return
    if _STATE["reachable"] is not True:
        await emit("pythia.health.ok", module="pythia")
    _STATE["reachable"] = True
    ollama = bool(h.get("ollama_available"))
    if ollama != _STATE["ollama"]:
        _STATE["ollama"] = ollama
        if ollama:
            await emit("pythia.health.ollama_up", module="pythia")
        else:
            await emit("pythia.health.degraded", module="pythia", level="warn")


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(20)
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("pythia_monitor_failed", error=repr(exc))


async def start() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="pythia_monitor")


async def stop() -> None:
    global _TASK
    if _TASK is not None:
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):
            pass
        _TASK = None
