"""Run one guarded auto-ingestion cycle over the live corpus.

A cycle chains the engine's existing steps (discover, dedupe, ingest, embed, enrich, reindex,
temporal re-stamp, graph-changed drain) into a self-running pipeline behind an app on/off toggle.
It adds no ingest or retrieval logic of its own: amendments go through ``refresh.refresh_one`` and
new acts through ``esbirka.ingest_act(amend=False)``. Because it changes a legal corpus unattended,
it is off by default and guarded at every stage; ``run_cycle`` describes the safeguards.

Run (from engine/): .venv/bin/python -m pythia.ingest.auto_ingest [--live | --dry-run]
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import uuid
from datetime import UTC, datetime

from pythia.config import get_settings
from pythia.db import close_pool, get_pool
from pythia.ingest import _resource_signals as rs

_log = logging.getLogger("pythia.ingest.auto_ingest")

# The ingestion_run ledger in the engine database, created on first use (IF NOT EXISTS) following the
# refresh.py _DDL convention (raw asyncpg DDL, uuid primary key, jsonb detail). It backs the
# /v1/ingestion/status and /v1/ingestion/history endpoints.
_DDL = """
CREATE TABLE IF NOT EXISTS ingestion_run (
    id uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    mode text,
    n_discovered int, n_ingested int, n_amended int, n_embedded int, n_enriched int, n_errors int,
    halted bool NOT NULL DEFAULT false, halt_reason text,
    defined_terms_before int, defined_terms_after int,
    detail jsonb);
CREATE INDEX IF NOT EXISTS ix_ingestion_run_started ON ingestion_run (started_at DESC);
"""

# Terminal discovery_queue status stamped once a candidate is ingested (status is a free-text column, so
# no schema change is needed). 'pending' rows are the work queue; 'ingested' rows are done.
_STATUS_INGESTED = "ingested"


async def _defined_terms_total(conn) -> int:
    """Return the corpus-wide defined_term row count, used by the defined-term loss tripwire."""
    return int(await conn.fetchval("SELECT count(*) FROM defined_term") or 0)


async def _coverage_gap_in_force(conn) -> int:
    """Return the number of non-anchor provisions with text that still lack a bge vector.

    Shown on the run status in the admin panel. Best-effort: returns -1 if the query fails (for example
    on a different schema), so a metric read never crashes the cycle.
    """
    try:
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM provision WHERE embedding_bge IS NULL AND text IS NOT NULL AND NOT is_anchor_only"
            )
            or 0
        )
    except Exception:  # noqa: BLE001 — metric only; a read failure must not abort the cycle
        return -1


def _run_snapshot(detail: dict) -> str | None:
    """Run the operator's pre-amendment snapshot hook (`snapshot_cmd`, e.g. `pg_dump pythia > …`).

    With no command configured (the default) nothing is snapshotted; a warning is logged and the run
    records that amendments ran without a snapshot. A failed snapshot is recorded but does not abort
    the cycle: whether a snapshot is a hard precondition is the operator's call. The status is stored
    in ingestion_run.detail['snapshot'].

    Returns:
        "ok", "failed", or None when no command is configured.
    """
    cmd = (get_settings().snapshot_cmd or "").strip()
    if not cmd:
        _log.warning(
            "auto-ingest: amends are running WITHOUT a configured snapshot_cmd — set "
            "PYTHIA_SNAPSHOT_CMD (e.g. a pg_dump pythia hook) for rollback safety"
        )
        detail["snapshot"] = "none_configured"
        return None
    try:
        subprocess.run(cmd, shell=True, check=True, timeout=600)
        detail["snapshot"] = "ok"
        _log.info("auto-ingest: pre-amend snapshot completed via snapshot_cmd")
        return "ok"
    except Exception as exc:  # noqa: BLE001
        detail["snapshot"] = f"failed: {exc!r}"[:200]
        _log.error("auto-ingest: snapshot_cmd FAILED: %r — amends proceeding without a fresh snapshot", exc)
        return "failed"


async def _pending_candidates(conn, limit: int) -> list[dict]:
    """Return pending discovery_queue rows, oldest first, for the dedupe stage.

    The limit makes a large backlog roll over to later cycles. Returns [] if discovery_queue does not
    exist yet (discovery has never run on this corpus).
    """
    try:
        rows = await conn.fetch(
            "SELECT id::text id, external_id, source, published_on FROM discovery_queue "
            "WHERE status = 'pending' ORDER BY seen_at ASC LIMIT $1",
            limit,
        )
    except Exception:  # noqa: BLE001 — table absent means nothing is pending
        return []
    return [dict(r) for r in rows]


async def _mark_ingested(conn, queue_id: str) -> None:
    await conn.execute("UPDATE discovery_queue SET status = $2 WHERE id = $1::uuid", queue_id, _STATUS_INGESTED)


async def run_cycle(dry_run: bool | None = None) -> dict:
    """Run one auto-ingestion cycle and return a summary dict.

    Every cycle that gets past the switches and the lock writes an ingestion_run row; a guard that
    trips mid-cycle (kill switch, low disk) records a halted run.

    Args:
        dry_run: None reads `auto_ingest_dry_run` from settings; True or False forces the mode. A dry
            run only discovers and deduplicates (mode='dry_run') and stops before any mutation.

    These short-circuits change nothing and take no lock:
        {"skipped": "disabled_env"}     the `auto_ingest_enabled` setting is off
        {"skipped": "disabled"}         the app switch file data/AUTO_INGEST_ENABLED is off or absent
        {"skipped": "already_running"}  another cycle holds the single-flight lock

    Safeguards:
      * Off by default, behind two switches: the engine setting `auto_ingest_enabled` (the
        code-level master switch) and the app-written `data/AUTO_INGEST_ENABLED` file (the live
        on/off control) must both be on, or the cycle returns before taking the lock or touching
        the database.
      * Dry run first: dry-run mode (the default unless `auto_ingest_dry_run` is false) only
        discovers and deduplicates and returns the plan; it never ingests, embeds, enriches or
        reindexes.
      * Single flight: a cross-process lock (data/AUTO_INGEST.lock) refuses a second concurrent
        cycle, so two embedding runs (two model loads) never overlap. A crashed run's lock is
        reclaimed after `lock_ttl_s`.
      * Kill switch between stages and between acts, via
        ``_resource_signals.check_between_batches``. Creating data/KILL_SWITCH halts cleanly:
        each amendment is its own transaction, and the deferred reindex simply does not run.
      * Memory pre-check: at or over the RAM ceiling the heavy embed and reindex stages are
        skipped; the acts stay ingested but unembedded and roll over to the next cycle. The corpus
        stays consistent and graph and lexical retrieval keep working.
      * Defined-term loss tripwire: the corpus-wide defined_term count is taken before ingest and
        again after enrichment; if it dropped, the run halts and raises an alarm. This sits on top
        of the per-act snapshot and restore inside ``refresh.refresh_one``.
      * One model load per cycle: ``embed_bge.embed_all`` and ``refresh._reindex`` run at most once.
      * Idempotent: discovery inserts with ON CONFLICT DO NOTHING, amendments are version-aware and
        non-destructive, embedding fills only missing vectors, and re-draining finds nothing, so
        re-running a cycle is safe.
    """
    s = get_settings()
    explicit = dry_run is not None  # the caller forced a mode (manual run) rather than the default
    if dry_run is None:
        dry_run = s.auto_ingest_dry_run
    # Mode label stored on the run row: 'dry_run' for a preview; for a live cycle, 'manual' when the
    # caller forced it (POST run-once with dry_run=false), otherwise 'auto' (a scheduled tick).
    mode = "dry_run" if dry_run else ("manual" if explicit else "auto")

    # ── Stage 1: both on/off switches (engine setting and app file); either off means do nothing. ──
    if not s.auto_ingest_enabled:
        return {"skipped": "disabled_env", "mode": mode}
    if not rs.auto_ingest_enabled_file():
        return {"skipped": "disabled", "mode": mode}

    # ── Stage 2: single-flight lock, taken before any database access. ──
    if not rs.acquire_lock(lock_ttl_s=s.lock_ttl_s):
        _log.info("auto-ingest: refused — another cycle is already running (lock contended)")
        return {"skipped": "already_running", "mode": mode}

    try:
        return await _run_locked(s, dry_run=dry_run, mode=mode)
    finally:
        rs.release_lock()


async def _run_locked(s, *, dry_run: bool, mode: str) -> dict:
    """Run the cycle body; called only while run_cycle holds the single-flight lock."""
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    detail: dict = {"trigger": mode, "stages": []}
    counts = {"n_discovered": 0, "n_ingested": 0, "n_amended": 0, "n_embedded": 0, "n_enriched": 0, "n_errors": 0}
    halted, halt_reason = False, None
    dt_before: int | None = None
    dt_after: int | None = None
    touched: list[str] = []  # external_ids ingested or amended this cycle (the enrichment scope)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        dt_before = await _defined_terms_total(conn)
        await conn.execute(
            """INSERT INTO ingestion_run (id, started_at, mode, defined_terms_before)
               VALUES ($1::uuid, $2, $3, $4)""",
            run_id,
            started,
            mode,
            dt_before,
        )

    # Lazy imports keep this module light to import (no torch or connector imports at load time).
    from pythia.ingest import discovery
    from pythia.ingest import refresh

    def _guard(stage: str) -> str | None:
        """Check the kill switch and free disk between stages; return a halt reason or None."""
        try:
            rs.check_between_batches()
        except rs.KillSwitchEngaged:
            return "killswitch"
        except rs.FreeDiskExhausted:
            return "disk"
        return None

    try:
        # ── Stage 3: discover. A dry run reads candidates without writing them to the queue. ──
        if hr := _guard("discover"):
            halted, halt_reason = True, hr
        else:
            if dry_run:
                cands = await discovery.discover(dry_run=True, cap=s.discover_cap)
                discovered = [{"external_id": c.external_id, "source": c.source} for c in cands]
            else:
                summ = await discovery.discover_publishers(cap=s.discover_cap)
                detail["discover"] = summ
                discovered = []  # the persisted pending rows are read back from the queue in dedupe
            counts["n_discovered"] = len(discovered) if dry_run else detail.get("discover", {}).get("persisted", 0)
            detail["stages"].append("discover")

        # ── Stage 4: dedupe against the corpus (new, amended or unchanged, via refresh.check_one). ──
        plan: list[dict] = []
        if not halted:
            if dry_run:
                # A dry run plans straight from the candidates just read (nothing was queued).
                cand_ids = [d["external_id"] for d in discovered][: s.max_acts_per_cycle]
            else:
                async with pool.acquire() as conn:
                    pending = await _pending_candidates(conn, s.max_acts_per_cycle)
                detail["pending"] = [p["external_id"] for p in pending]
                cand_ids = [p["external_id"] for p in pending]
                detail["_pending_rows"] = {p["external_id"]: p["id"] for p in pending}
            for ext in cand_ids:
                try:
                    chk = await refresh.check_one(ext)
                except Exception as exc:  # noqa: BLE001 — one candidate's check failing must not abort triage
                    counts["n_errors"] += 1
                    plan.append({"external_id": ext, "action": "error", "error": repr(exc)[:140]})
                    continue
                if chk.get("status") == "absent":
                    plan.append({"external_id": ext, "action": "ingest_new"})
                elif chk.get("changed"):
                    plan.append({"external_id": ext, "action": "amend"})
                else:
                    plan.append({"external_id": ext, "action": "unchanged"})
            detail["plan"] = plan
            detail["stages"].append("dedupe")

        # ── Dry run stops here and returns the plan; nothing is changed. ──
        if dry_run and not halted:
            return await _finish(
                run_id,
                started,
                mode,
                counts,
                detail,
                halted=False,
                halt_reason=None,
                dt_before=dt_before,
                dt_after=dt_before,
                plan=plan,
            )
        if halted:
            return await _finish(
                run_id,
                started,
                mode,
                counts,
                detail,
                halted=True,
                halt_reason=halt_reason,
                dt_before=dt_before,
                dt_after=dt_before,
                plan=plan,
            )

        # ── Live path (stages 5 to 10) ──────────────────────────────────────────────────────────────
        actionable = [p for p in plan if p["action"] in ("ingest_new", "amend")]
        if any(p["action"] == "amend" for p in actionable):
            _run_snapshot(detail)  # pre-amendment snapshot hook (warns if unconfigured)

        # ── Stage 5: ingest new acts and amend changed ones, checking the kill switch between acts. ──
        from pythia.ingest.esbirka import CzechActSpec, ingest_act  # lazy: connector import

        pending_ids = detail.get("_pending_rows", {})
        for p in actionable:
            if hr := _guard("ingest_act"):
                halted, halt_reason = True, hr
                break
            ext = p["external_id"]
            try:
                if p["action"] == "amend":
                    # Non-destructive amend with defined-term snapshot, restore and alarm; the
                    # reindex is deferred so it runs once per cycle.
                    await refresh.refresh_one(ext, reindex=False)
                    counts["n_amended"] += 1
                else:
                    # New act. Only Collection of Laws (Sbírka) acts can be ingested automatically
                    # from an external_id: the amend path rebuilds a CzechActSpec from stored
                    # metadata, but a new act has none, so a minimal spec is derived from the id.
                    # EU and ČNB acts are not handled here.
                    number, year = ext.split("/")
                    spec = CzechActSpec(int(year), int(number), ext, ext, "cz-act", "act")
                    await ingest_act(spec, amend=False)
                    counts["n_ingested"] += 1
                touched.append(ext)
                if not dry_run:
                    qid = pending_ids.get(ext)
                    if qid:
                        async with pool.acquire() as conn:
                            await _mark_ingested(conn, qid)
            except Exception as exc:  # noqa: BLE001 — one act failing must not abort the whole cycle
                counts["n_errors"] += 1
                detail.setdefault("act_errors", []).append({"external_id": ext, "error": repr(exc)[:140]})
                _log.error("auto-ingest: %s %s FAILED: %r", p["action"], ext, exc)
        detail["touched"] = touched
        detail["stages"].append("ingest")

        # ── Stages 6 to 8: embed, enrich, reindex; gated by the memory pre-check (one model load). ──
        if not halted and touched:
            if hr := _guard("embed"):
                halted, halt_reason = True, hr
            elif not rs.memory_ok(s.mem_precheck_ceiling_pct):
                # Over the RAM ceiling: leave the acts unembedded and embed them in the next cycle.
                detail["memory_deferred"] = True
                detail["memory_percent"] = rs.memory_percent()
                _log.warning(
                    "auto-ingest: RAM%% >= %.0f ceiling — deferring embed/reindex; "
                    "%d act(s) ingested-but-unembedded roll to next cycle",
                    s.mem_precheck_ceiling_pct,
                    len(touched),
                )
            else:
                try:
                    from pythia.ingest.embed_bge import embed_all  # lazy: the only bge model load

                    counts["n_embedded"] = await embed_all(
                        only_missing=True, batch=s.embed_batch, enc_batch=s.enc_batch
                    )
                    detail["stages"].append("embed")
                    # Enrich only the touched acts, so new provisions get their defined terms extracted.
                    if not (hr := _guard("enrich")):
                        try:
                            from pythia.ingest.enrich_definitions import enrich  # lazy

                            await enrich(acts=touched, limit=10_000, rematerialize=True, all_missing=False)
                            counts["n_enriched"] = len(touched)
                            detail["stages"].append("enrich")
                        except Exception as exc:  # noqa: BLE001 — enrich is best-effort; reindex still runs
                            counts["n_errors"] += 1
                            detail.setdefault("act_errors", []).append({"stage": "enrich", "error": repr(exc)[:140]})
                            _log.error("auto-ingest: enrich FAILED: %r", exc)
                    else:
                        halted, halt_reason = True, hr
                    # Reindex once per cycle: embed missing, re-link, materialize, roles, the
                    # graph-intelligence integrity check, and clear the PageRank cache.
                    if not halted and not (hr := _guard("reindex")):
                        reidx = await refresh._reindex()
                        detail["reindex"] = {
                            "embedded": reidx.get("embedded"),
                            "edges": reidx.get("edges"),
                            "invariants_ok": reidx.get("invariants_ok"),
                        }
                        detail["stages"].append("reindex")
                    elif hr:
                        halted, halt_reason = True, hr
                except rs.KillSwitchEngaged:
                    halted, halt_reason = True, "killswitch"
                except rs.FreeDiskExhausted:
                    halted, halt_reason = True, "disk"

        # ── Stage 9: temporal re-stamp (cheap and offline; keeps in-force status from going stale). ──
        if not halted and touched and "reindex" in detail.get("stages", []):
            if not (hr := _guard("temporal")):
                try:
                    from pythia.ingest.scheduler import run_temporal  # lazy: avoid import cycle at module load

                    await run_temporal(apply=True)
                    detail["stages"].append("temporal")
                except Exception as exc:  # noqa: BLE001 — temporal is best-effort
                    detail.setdefault("act_errors", []).append({"stage": "temporal", "error": repr(exc)[:140]})
            else:
                halted, halt_reason = True, hr

        # ── Stage 10: drain graph-changed events (flags the GNN as stale on drift; never retrains). ──
        if not halted:
            try:
                from pythia.ingest.graph_changed_consumer import consume_graph_changed  # lazy

                drain = await consume_graph_changed(dry_run=False)
                detail["graph_changed_drain"] = {
                    "enabled": drain.get("enabled"),
                    "drained": drain.get("drained"),
                    "stale": drain.get("stale"),
                }
                detail["stages"].append("graph-changed-drain")
            except Exception as exc:  # noqa: BLE001 — drain is best-effort
                detail.setdefault("act_errors", []).append({"stage": "drain", "error": repr(exc)[:140]})

        # ── Defined-term loss tripwire: recount defined_term corpus-wide; a drop halts and alarms. ──
        async with pool.acquire() as conn:
            dt_after = await _defined_terms_total(conn)
        if dt_after is not None and dt_before is not None and dt_after < dt_before:
            halted, halt_reason = True, "defined_terms_drop"
            detail["defined_terms_drop"] = {"before": dt_before, "after": dt_after, "lost": dt_before - dt_after}
            _log.error(
                "auto-ingest: H13 STOP-THE-WORLD — defined_term count DROPPED %d→%d (lost %d). "
                "HALTING run %s and alarming.",
                dt_before,
                dt_after,
                dt_before - dt_after,
                run_id,
            )
            async with pool.acquire() as conn:
                await refresh._emit(
                    conn,
                    "auto-ingest-halt",
                    {"run_id": run_id, "reason": "defined_terms_drop", "before": dt_before, "after": dt_after},
                )

        return await _finish(
            run_id,
            started,
            mode,
            counts,
            detail,
            halted=halted,
            halt_reason=halt_reason,
            dt_before=dt_before,
            dt_after=dt_after,
            plan=plan,
        )

    except Exception as exc:  # noqa: BLE001 — record any uncaught error as a halted run, never lose it
        counts["n_errors"] += 1
        detail["fatal"] = repr(exc)[:300]
        _log.exception("auto-ingest: cycle %s FAILED", run_id)
        return await _finish(
            run_id,
            started,
            mode,
            counts,
            detail,
            halted=True,
            halt_reason="error",
            dt_before=dt_before,
            dt_after=dt_after,
            plan=detail.get("plan", []),
        )


async def _finish(run_id, started, mode, counts, detail, *, halted, halt_reason, dt_before, dt_after, plan) -> dict:
    """Finalize the ingestion_run row, emit the summary event, and return the cycle summary."""
    finished = datetime.now(UTC)
    detail.pop("_pending_rows", None)  # internal mapping; keep it out of the stored detail
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ingestion_run SET finished_at = $2, n_discovered = $3, n_ingested = $4, n_amended = $5,
                      n_embedded = $6, n_enriched = $7, n_errors = $8, halted = $9, halt_reason = $10,
                      defined_terms_after = $11, detail = $12::jsonb WHERE id = $1::uuid""",
            run_id,
            finished,
            counts["n_discovered"],
            counts["n_ingested"],
            counts["n_amended"],
            counts["n_embedded"],
            counts["n_enriched"],
            counts["n_errors"],
            halted,
            halt_reason,
            dt_after,
            json.dumps(detail, ensure_ascii=False, default=str),
        )
        # Summary event on the nexus_event bus that the app and UI subscribe to.
        from pythia.ingest.refresh import _emit

        await _emit(
            conn,
            "auto-ingest.finished" if not halted else "auto-ingest.halt",
            {"run_id": run_id, "mode": mode, "halted": halted, "halt_reason": halt_reason, **counts},
        )
    return {
        "run_id": run_id,
        "mode": mode,
        "halted": halted,
        "halt_reason": halt_reason,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "defined_terms_before": dt_before,
        "defined_terms_after": dt_after,
        "plan": plan,
        **counts,
    }


async def _amain(dry_run: bool | None) -> dict:
    try:
        return await run_cycle(dry_run=dry_run)
    finally:
        await close_pool()


def main(argv: list[str]) -> int:
    """Run one cycle from the command line and return the process exit code.

    `--live` forces a live cycle and `--dry-run` a dry run; with neither flag the mode comes from the
    `auto_ingest_dry_run` setting. Exits 0 on success, 2 if the cycle halted, 1 on a crash.
    """
    dry_run: bool | None = None
    if "--live" in argv:
        dry_run = False
    elif "--dry-run" in argv:
        dry_run = True
    try:
        res = asyncio.run(_amain(dry_run))
    except Exception as exc:  # noqa: BLE001
        print(f"auto-ingest cycle FAILED: {exc!r}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, default=str, indent=2))
    return 0 if not res.get("halted") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
