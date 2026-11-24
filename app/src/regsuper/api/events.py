"""Server-sent events stream that feeds the frontend's project log panel.

The panel's Activity tab renders `friendly_message` and its Diagnostics tab renders `code` and
`diagnostic_message`. Recent events are replayed first, so a freshly opened panel shows history
and not only events from that moment on.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from regsuper.observability.events import (
    Event,
    replay_recent,
    subscribe,
    unsubscribe,
)

router = APIRouter()

_KEEPALIVE_SECONDS = 15
_REPLAY_LIMIT = 50
_QUEUE_MAXSIZE = 500


async def _event_stream(project_id: str) -> AsyncGenerator[dict[str, Any], None]:
    """Replay recent events, then tail live ones; releases the subscriber slot on disconnect."""
    # 1. Backfill from the structlog file (bounded, see events.replay_recent).
    try:
        recent = await replay_recent(project_id, limit=_REPLAY_LIMIT)
    except Exception:
        recent = []
    for past in recent:
        yield {"data": json.dumps(past)}

    # 2. Live tail via the in-process pub/sub.
    queue: asyncio.Queue[Event] = subscribe(project_id, maxsize=_QUEUE_MAXSIZE)
    try:
        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                yield {"data": json.dumps(evt)}
            except TimeoutError:
                # Keepalive so browsers stay subscribed and proxies don't close an idle connection.
                yield {"event": "ping", "data": ""}
    finally:
        unsubscribe(project_id, queue)


@router.get("/api/projects/{project_id}/events")
async def project_events(project_id: UUID) -> EventSourceResponse:
    """Stream a project's pipeline events as server-sent events.

    Replays the project's 50 most recent events, then streams each new event as the analysis
    pipeline emits it. A `ping` event is sent after 15 seconds of silence so intermediaries
    don't drop the connection.
    """
    return EventSourceResponse(_event_stream(str(project_id)))
