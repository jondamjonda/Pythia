"""Monitoring scan log and operator approval of discovered acts (the human-in-the-loop gate).

Backs the sidecar's ``/v1/monitoring/*`` endpoints. ``scan_log`` reads what the watcher
(``ingest/discovery.py``) recorded in the operational ``discovery_queue`` table and what versioning has
actually changed; ``record_decision`` stores an operator's approve or reject decision. Neither function
ever ingests anything, because this is a live legal corpus.
"""

from __future__ import annotations

import json as _json
from typing import Any
from uuid import UUID

from pythia.db import get_pool

# Per-list cap, so the scan-log payload stays bounded however long the watcher has been running.
_CAP = 100

# nexus_event kinds meaning "the graph moved and the GNN view may be stale", shown on the UI's staleness
# rail. amend() emits 'amended' (audit and change-diff provenance) and 'reindex-required' on a real version
# change, so an amendment shows up the moment it lands; 'graph-changed' comes from the refresh flow.
_STALE_KINDS = ("gnn-stale", "graph-changed", "reindex-required", "amended")


def _jsonb(v: Any) -> dict:
    """Return a jsonb value as a dict; asyncpg yields a str when no codec is registered."""
    if isinstance(v, str):
        try:
            return _json.loads(v) or {}
        except ValueError:
            return {}
    return v or {}


async def _table_exists(conn, name: str) -> bool:
    """Return True if the operational table exists.

    discovery_queue is created lazily by the watcher's first write, and the watcher is off by default
    and may never have run. On a fresh database the table is absent, so every read of it is guarded
    here and degrades to an empty result rather than an error.
    """
    return (await conn.fetchval("SELECT to_regclass($1)", f"public.{name}")) is not None


async def scan_log(*, cap: int = _CAP) -> dict[str, list[dict]]:
    """Return the monitoring scan log as three read-only feeds, each capped at `cap` rows.

    Only SELECTs run, over discovery_queue, provision_version, document_version and nexus_event.

    - ``discovery_queue``: pending, approved and rejected acts the watcher detected, newest first
      (empty if the table does not exist yet).
    - ``recent_version_events``: what versioning actually changed: real amendments (provision_version
      rows with version_no > 1) and document_version rows, merged newest first by detected_at.
    - ``stale_signals``: recent nexus_event rows whose kind signals that the graph moved or the GNN
      view is stale.

    This is the log the operator reads before deciding; nothing is written.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1. discovery_queue (an absent table gives an empty list, never an error)
        queue: list[dict] = []
        if await _table_exists(conn, "discovery_queue"):
            rows = await conn.fetch(
                """SELECT id::text AS id, external_id, published_on, source, status, seen_at, metadata
                     FROM discovery_queue
                    ORDER BY seen_at DESC
                    LIMIT $1""",
                cap,
            )
            queue = [
                {
                    "id": r["id"],
                    "external_id": r["external_id"],
                    "published_on": r["published_on"].isoformat() if r["published_on"] else None,
                    "source": r["source"],
                    "status": r["status"],
                    "seen_at": r["seen_at"].isoformat() if r["seen_at"] else None,
                    "metadata": _jsonb(r["metadata"]),
                }
                for r in rows
            ]

        # 2. recent version events: real amendments (provision_version > 1) and document_version rows
        pv_rows = await conn.fetch(
            """SELECT 'provision_version' AS event_type, sd.external_id, pv.provision_label AS label,
                      pv.version_no, pv.valid_from, pv.valid_to, pv.is_head, NULL::text AS change_summary,
                      pv.detected_at
                 FROM provision_version pv
                 JOIN source_document sd ON sd.id = pv.source_document_id
                WHERE pv.version_no > 1
                ORDER BY pv.detected_at DESC
                LIMIT $1""",
            cap,
        )
        dv_rows = await conn.fetch(
            """SELECT 'document_version' AS event_type, external_id, NULL::text AS label,
                      NULL::int AS version_no, valid_from, valid_to, NULL::boolean AS is_head,
                      change_summary, detected_at
                 FROM document_version
                ORDER BY detected_at DESC
                LIMIT $1""",
            cap,
        )
        merged = list(pv_rows) + list(dv_rows)
        merged.sort(key=lambda r: r["detected_at"], reverse=True)
        recent_version_events = [
            {
                "event_type": r["event_type"],
                "external_id": r["external_id"],
                "label": r["label"],
                "version_no": r["version_no"],
                "valid_from": r["valid_from"].isoformat() if r["valid_from"] else None,
                "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
                "is_head": r["is_head"],
                "change_summary": r["change_summary"],
                "detected_at": r["detected_at"].isoformat(),
            }
            for r in merged[:cap]
        ]

        # 3. stale signals: recent graph-moved and gnn-stale nexus_event rows
        ne_rows = await conn.fetch(
            """SELECT kind, payload, created_at
                 FROM nexus_event
                WHERE kind = ANY($1::text[])
                ORDER BY created_at DESC
                LIMIT $2""",
            list(_STALE_KINDS),
            cap,
        )
        stale_signals = [
            {"kind": r["kind"], "payload": _jsonb(r["payload"]), "created_at": r["created_at"].isoformat()}
            for r in ne_rows
        ]

    return {"discovery_queue": queue, "recent_version_events": recent_version_events, "stale_signals": stale_signals}


async def record_decision(queue_id: str, decision: str, *, note: str | None = None) -> dict | None:
    """Record an operator's approve or reject decision on a discovery_queue row.

    This never ingests. It performs a single UPDATE on the operational ``discovery_queue`` table,
    setting ``status`` and stamping the note and decided_at into the row's ``metadata`` jsonb. It
    touches no corpus table, calls no discover, amend, embed, reindex or materialise path, and emits
    no nexus_event. Approving an item is a note for a human, not a trigger; nothing acts on an
    approved row yet.

    `decision` must be 'approved' or 'rejected'; the caller validates it and it is checked again here.
    Returns the updated row as a dict, or None when the id is malformed, no such row exists, or the
    table does not exist yet (the endpoint maps None to 404).
    """
    decision = (decision or "").strip().lower()
    if decision not in ("approved", "rejected"):  # defence in depth; the caller validates too
        raise ValueError(f"decision must be 'approved' or 'rejected', got {decision!r}")
    try:
        qid = UUID(str(queue_id))
    except (ValueError, AttributeError, TypeError):
        return None  # malformed id: no such row (404)

    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _table_exists(conn, "discovery_queue"):  # watcher never ran, so nothing to decide
            return None
        # The only write in this module. discovery_queue is the watcher's operational queue, not a corpus
        # table, so this can never alter a provision. The jsonb merge (||) keeps existing metadata and adds
        # the audit fields (decision, note, decided_at).
        row = await conn.fetchrow(
            """UPDATE discovery_queue
                  SET status = $2,
                      metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object(
                          'decision', $2::text,
                          'note', $3::text,
                          'decided_at', now()::text)
                WHERE id = $1
            RETURNING id::text AS id, external_id, published_on, source, status, seen_at, metadata""",
            qid,
            decision,
            note,
        )
    if row is None:
        return None  # no such queue row (404)
    return {
        "id": row["id"],
        "external_id": row["external_id"],
        "published_on": row["published_on"].isoformat() if row["published_on"] else None,
        "source": row["source"],
        "status": row["status"],
        "seen_at": row["seen_at"].isoformat() if row["seen_at"] else None,
        "metadata": _jsonb(row["metadata"]),
    }
