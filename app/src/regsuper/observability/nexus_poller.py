"""Bridge the engine's event store to the app's event bus.

The Pythia engine (Nexus) runs as a separate process with its own database. This poller
fetches recent rows from its `nexus_event` table and re-emits the new ones as `module="nexus"`
events on the `__system__` channel, so the control room's Nexus log and alarm bar show ingest,
reindex, materialize and check activity live. The panel's gauges poll `/admin/api/nexus/stats`
directly; this module only bridges events.
"""

from __future__ import annotations

import asyncio

from regsuper.config import get_settings
from regsuper.observability.events import emit
from regsuper.observability.logging_config import get_logger
from regsuper.pythia import client as pythia_client

_log = get_logger(__name__)
_TASK: asyncio.Task | None = None
_LAST_TS: str | None = None  # watermark: created_at of the newest event already emitted


def _scalars(payload: dict) -> dict:
    return {k: v for k, v in (payload or {}).items() if isinstance(v, (str, int, float, bool))}


async def _poll_once() -> None:
    global _LAST_TS
    s = get_settings()
    if not (s.pythia_enabled and s.pythia_url):
        return
    try:
        events = await pythia_client.nexus_events(limit=25)  # newest-first
    except Exception:
        return
    if not events:
        return
    if _LAST_TS is None:
        _LAST_TS = events[0]["created_at"]  # first poll: set the watermark, don't replay history
        return
    new = [e for e in events if e["created_at"] > _LAST_TS]
    for e in reversed(new):  # oldest-first so the log reads in order
        kind = str(e.get("kind") or "nexus.event")
        code = kind if not kind[:1].isdigit() else f"nexus.{kind}"
        await emit(code, module="nexus", **_scalars(e.get("payload") or {}))
        _LAST_TS = e["created_at"]


async def _loop() -> None:
    while True:
        try:
            await asyncio.sleep(30)
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("nexus_poller_failed", error=repr(exc))


async def start() -> None:
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_loop(), name="nexus_poller")


async def stop() -> None:
    global _TASK
    if _TASK is not None:
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):
            pass
        _TASK = None
