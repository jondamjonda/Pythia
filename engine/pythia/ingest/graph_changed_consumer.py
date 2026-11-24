"""Graph-change monitor: flag the GNN checkpoint as stale once the graph has drifted far enough.

Drains the `graph-changed` events the refresh pipeline (`refresh.py`) emits when it amends an act
and, if the live graph has drifted far enough from the last persisted intelligence build, emits a
single `gnn-stale` event. It never retrains and never mutates the graph: retraining stays a
deliberate operator decision, not an automatic consequence of detecting drift. Off by default
(`graph_changed_monitor_enabled`) and a dry run by default.
"""

from __future__ import annotations

from pythia.config import get_settings
from pythia.db import get_pool
from pythia.graph.intelligence import current_fingerprint, last_fingerprint
from pythia.ingest.refresh import _emit

# The events drained (written on an amendment) and the one emitted. Both source kinds mean "the
# graph moved":
#   - 'graph-changed': emitted by refresh.refresh_one after it reindexes.
#   - 'reindex-required': emitted by provision_versioning.amend() as soon as a real version change
#     lands, with or without a full refresh reindex. Draining it means a direct amend, not only the
#     refresh CLI, also feeds the drift check.
_SOURCE_KINDS = ("graph-changed", "reindex-required")
_STALE_KIND = "gnn-stale"

# Drift threshold: the fraction of the last build's edge count by which the live edge count must
# differ before the checkpoint counts as stale. Deliberately high (10%): a few amended provisions
# rewiring a few edges is normal churn the GNN tolerates, so `gnn-stale` is raised only once the graph
# has moved enough that the trained representations are plausibly out of date. Lowering it is an
# operator decision, and even past the threshold retraining stays manual.
DRIFT_THRESHOLD: float = 0.10

# edge_hash changes on any edge-set change, including a rewire or reweight at constant count, so a hash
# mismatch is reported as drift in its own right. The emit is still gated on the count threshold to
# stay conservative.


def _drift(live: dict, last: dict | None) -> dict:
    """Compare the live fingerprint with the last persisted build fingerprint (pure function).

    Returns {stale, edge_delta, edge_fraction, hash_changed, reason}. `stale` is true only when the
    edge count has moved by at least DRIFT_THRESHOLD of the last build's count. Without a prior build
    fingerprint drift cannot be judged, so the result is not stale; a never-built layer is an
    operator concern, not this monitor's.
    """
    if not last or last.get("edge_count") in (None, 0):
        return {
            "stale": False,
            "edge_delta": 0,
            "edge_fraction": 0.0,
            "hash_changed": False,
            "reason": "no_prior_build",
        }
    last_count = int(last["edge_count"])
    live_count = int(live["edge_count"])
    delta = abs(live_count - last_count)
    fraction = delta / last_count
    hash_changed = last.get("edge_hash") != live.get("edge_hash")
    stale = fraction >= DRIFT_THRESHOLD
    reason = (
        "drift_over_threshold" if stale else ("within_tolerance" if not hash_changed else "hash_drift_subthreshold")
    )
    return {
        "stale": stale,
        "edge_delta": delta,
        "edge_fraction": round(fraction, 6),
        "hash_changed": hash_changed,
        "reason": reason,
    }


async def consume_graph_changed(*, dry_run: bool = True) -> dict:
    """Drain pending graph-change events and emit one `gnn-stale` event if drift passes the threshold.

    Does nothing and returns `{"enabled": False}` unless `graph_changed_monitor_enabled` is set.
    When enabled it selects the pending source events (consumed_at IS NULL, oldest first), compares
    the live graph fingerprint with the last persisted intelligence build and, if drift is at least
    DRIFT_THRESHOLD and `dry_run` is false, marks those rows consumed and emits exactly one
    `gnn-stale` event via `refresh._emit` (payload: live fingerprint, number drained, drift
    summary). With `dry_run=True` (the default) it only computes and returns the summary. It never
    retrains.

    Returns `{enabled, drained, pending, stale, fingerprint, drift, emitted, dry_run}`.
    """
    settings = get_settings()
    if not settings.graph_changed_monitor_enabled:
        return {"enabled": False}

    pool = await get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            "SELECT id, payload, created_at FROM nexus_event "
            "WHERE kind = ANY($1::text[]) AND consumed_at IS NULL ORDER BY created_at ASC",
            list(_SOURCE_KINDS),
        )
        live = await current_fingerprint(conn)
        last = await last_fingerprint(conn)
        drift = _drift(live, last)
        ids = [r["id"] for r in pending]
        n = len(ids)

        # The write is guarded twice: never on a dry run, never without real drift. It is idempotent
        # (draining again finds nothing pending), and marking consumed plus emitting share one
        # transaction so a reader never sees drained events without the stale signal, or vice versa.
        emitted = False
        if drift["stale"] and not dry_run and n:
            async with conn.transaction():
                await conn.execute("UPDATE nexus_event SET consumed_at = now() WHERE id = ANY($1::uuid[])", ids)
                await _emit(
                    conn,
                    _STALE_KIND,
                    {
                        "fingerprint": live,
                        "n_drained": n,
                        "drift": drift,
                        "note": "graph drifted past threshold — retrain is a deliberate operator act",
                    },
                )
            emitted = True

    return {
        "enabled": True,
        "drained": (n if emitted else 0),
        "pending": n,
        "stale": drift["stale"],
        "fingerprint": live,
        "drift": drift,
        "emitted": emitted,
        "dry_run": dry_run,
    }
