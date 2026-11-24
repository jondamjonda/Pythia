"""Schedulable entry point for the engine's self-maintenance jobs; this module owns the schedule.

Jobs: ``temporal`` (daily, network-free recompute of the stored temporal status), ``refresh`` (weekly
re-check of every stored Sbírka act against e-Sbírka) and the automatic-ingestion jobs ``discover``,
``auto-ingest`` and ``graph-changed-drain``, each inert until its feature flag (and, for auto-ingest,
the operator toggle) is switched on. See the ``run_*`` functions for details.

Schedule with cron or with the daemon, never both for the same job:

  1) Cron the CLI (recommended: no long-lived process, survives reboots). Example crontab lines:

       # Daily temporal-status recompute (04:10 local time); cheap, no network
       10 4 * * *  cd /ABSOLUTE/PATH/TO/engine && .venv/bin/python -m pythia.ingest.scheduler temporal --apply >> scheduler.log 2>&1
       # Weekly corpus refresh (Sundays 04:30); re-resolve and reindex changed acts
       30 4 * * 0  cd /ABSOLUTE/PATH/TO/engine && .venv/bin/python -m pythia.ingest.scheduler refresh >> scheduler.log 2>&1

     ``python -m pythia.ingest.scheduler --print-crontab`` prints the lines for every job with the path
     filled in.

  2) A long-lived APScheduler process, e.g. alongside the sidecar. Needs the optional ``sched`` extra
     (``pip install -e '.[sched]'``):

       .venv/bin/python -m pythia.ingest.scheduler daemon   # blocks; runs temporal, refresh, discover, auto-ingest

CLI (from engine/), as ``python -m pythia.ingest.scheduler <command>``:
  temporal [--apply]               recompute the stored status (dry run unless --apply)
  refresh [--no-reindex]           check and refresh every stored act (always applies)
  daily [--apply] [--no-refresh]   temporal, then refresh (the default command)
  discover                         persist pending discovery rows (inert unless enabled)
  auto-ingest [--live]             one guarded auto-ingest cycle (dry run by default)
  graph-changed-drain              turn graph-changed events into gnn-stale (inert unless enabled)
  daemon                           APScheduler loop (needs the sched extra)
  --print-crontab                  print ready-to-paste crontab lines

The exit code is non-zero if a job raised, so cron mail or a CI wrapper surfaces failures.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

from pythia.config import get_settings
from pythia.db import close_pool
from pythia.ingest.provision_versioning import recompute_provision_temporal_status, recompute_temporal_status
from pythia.ingest.refresh import refresh_all

# The engine directory (two levels above pythia/ingest/), so printed crontab lines carry an absolute path.
_BUILD_DIR = pathlib.Path(__file__).resolve().parent.parent.parent

# The canonical schedule (cron fields are local time). Kept here so the crontab lines and the APScheduler
# daemon agree; change the cadence only here.
_CRON_TEMPORAL = ("10", "4", "*", "*", "*")  # daily 04:10
_CRON_REFRESH = ("30", "4", "*", "*", "0")  # weekly, Sundays 04:30


def _cron_fields(spec: str, fallback: tuple[str, str, str, str, str]) -> tuple[str, str, str, str, str]:
    """Parse a five-field cron string ('m h dom mon dow') from config into a tuple.

    A malformed value falls back to the documented default, so a bad setting can never crash the scheduler.
    """
    parts = (spec or "").split()
    return tuple(parts) if len(parts) == 5 else fallback  # type: ignore[return-value]


# Automatic-ingestion cadence, read from config so the operator can retune it without code changes:
#   discover    : daily 03:30 (persist pending discovery rows; cheap)
#   auto-ingest : weekly, Sundays 04:30 (the full guarded cycle; dry run until switched to live)
# graph-changed-drain runs at the end of each auto-ingest cycle (inside run_cycle) and is also a standalone
# CLI command, so an operator can drain on demand.
_CRON_DISCOVER = _cron_fields(get_settings().cron_discover, ("30", "3", "*", "*", "*"))
_CRON_AUTO_INGEST = _cron_fields(get_settings().cron_auto_ingest, ("30", "4", "*", "*", "0"))


async def run_temporal(*, apply: bool) -> dict:
    """Re-derive the stored temporal status facets; network-free and idempotent, dry run unless `apply`.

    The document-level recompute turns NOT_YET_EFFECTIVE into IN_FORCE, and IN_FORCE past valid_to into
    REPEALED, for documents whose effect or end date has passed. Retrieval masks already correct for
    this at query time; the job refreshes the stored status the UI shows. Cheap; run it daily.

    With ``temporal_substrate_enabled`` off (the default) this recomputes only the document-level status
    and returns ``recompute_temporal_status``'s result as is; ``provision.pit_status`` is neither created
    nor touched. With the flag on it also derives the provision-level ``pit_status`` (IN_FORCE, SUPERSEDED
    or NOT_YET_IN_FORCE from each head version window) and returns both facets as
    ``{"doc": ..., "provision": ...}``. Both are pure projections of stored dates.
    """
    if not get_settings().temporal_substrate_enabled:
        return await recompute_temporal_status(apply=apply)  # flag off: document-level status only
    doc = await recompute_temporal_status(apply=apply)
    prov = await recompute_provision_temporal_status(apply=apply)
    return {"doc": doc, "provision": prov}


async def run_refresh(*, reindex: bool = True) -> dict:
    """Check and refresh every stored act: re-resolve, re-ingest what changed, reindex once.

    Re-resolves each stored Sbírka act's current consolidated e-Sbírka version and re-ingests only what
    changed (idempotent; a no-op when nothing changed). Uses the network and is heavier than the temporal
    job: weekly by default, and ``daily`` also runs it unless --no-refresh is given.
    """
    return await refresh_all(reindex=reindex)


async def run_daily(*, apply: bool, do_refresh: bool, reindex: bool = True) -> dict:
    """Run the daily maintenance pass: the temporal recompute, then optionally the corpus refresh.

    Temporal runs first because it is cheap and never touches the network, so a flaky e-Sbírka fetch
    cannot block the status update.
    """
    out: dict = {}
    out["temporal"] = await run_temporal(apply=apply)
    if do_refresh:
        out["refresh"] = await run_refresh(reindex=reindex)
    return out


async def run_discover() -> dict:
    """Persist pending discovery rows for the configured publishers (the ``discover`` job).

    Inert (returns ``{"enabled": False}``) unless ``discovery_enabled`` is set; persisting is idempotent.
    """
    from pythia.ingest.discovery import discover_publishers

    return await discover_publishers(cap=get_settings().discover_cap)


async def run_auto_ingest(*, dry_run: bool | None = None) -> dict:
    """Run one full guarded auto-ingest cycle, including the graph-changed drain (the ``auto-ingest`` job).

    `dry_run` defaults to the ``auto_ingest_dry_run`` setting. The cycle returns ``{"skipped": ...}``
    while the operator's on/off toggle is off, so a scheduled tick changes nothing until it is switched on.
    """
    from pythia.ingest.auto_ingest import run_cycle

    return await run_cycle(dry_run=dry_run)


async def run_graph_changed_drain() -> dict:
    """Drain graph-changed and reindex-required events, raising gnn-stale on drift (never retrains).

    Inert unless ``graph_changed_monitor_enabled`` is set. Also runs at the end of every auto-ingest cycle.
    """
    from pythia.ingest.graph_changed_consumer import consume_graph_changed

    return await consume_graph_changed(dry_run=False)


def _print_crontab() -> None:
    b = str(_BUILD_DIR)
    tmin, thr, *_ = _CRON_TEMPORAL
    rmin, rhr, _d, _mon, rdow = _CRON_REFRESH
    dmin, dhr, _dd, _dmon, _ddow = _CRON_DISCOVER
    amin, ahr, _ad, _amon, adow = _CRON_AUTO_INGEST
    print("# Nexus daily temporal-status recompute — cheap, no network")
    print(
        f"{tmin} {thr} * * *  cd {b} && .venv/bin/python -m pythia.ingest.scheduler temporal --apply "
        f">> scheduler.log 2>&1"
    )
    print("# Nexus weekly corpus refresh — re-resolve + reindex changed acts")
    print(
        f"{rmin} {rhr} * * {rdow}  cd {b} && .venv/bin/python -m pythia.ingest.scheduler refresh >> scheduler.log 2>&1"
    )
    print(
        "# Nexus daily discovery — persist newly-published / version-changed candidates (INERT unless "
        "PYTHIA_DISCOVERY_ENABLED=1)"
    )
    print(f"{dmin} {dhr} * * *  cd {b} && .venv/bin/python -m pythia.ingest.scheduler discover >> scheduler.log 2>&1")
    print(
        "# Nexus weekly AUTO-INGEST cycle — discover→ingest→embed→reindex (INERT/dry-run unless the "
        "app toggle + PYTHIA_AUTO_INGEST_ENABLED are ON)"
    )
    print(
        f"{amin} {ahr} * * {adow}  cd {b} && .venv/bin/python -m pythia.ingest.scheduler auto-ingest "
        f">> scheduler.log 2>&1"
    )


def _run_daemon() -> None:
    """Run the blocking in-process APScheduler loop.

    Cron is the default; this is for deployments that want a daemon. APScheduler is imported lazily so
    the ``sched`` extra stays optional for the cron path.
    """
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:  # noqa: BLE001
        raise SystemExit(
            "daemon mode needs APScheduler — install the optional extra:  pip install -e '.[sched]'\n"
            f"(or use cron instead: python -m pythia.ingest.scheduler --print-crontab)  [{exc}]"
        )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    sched = AsyncIOScheduler(event_loop=loop)
    tmin, thr, *_ = _CRON_TEMPORAL
    rmin, rhr, _d, _mon, rdow = _CRON_REFRESH
    dmin, dhr, _dd, _dmon, _ddow = _CRON_DISCOVER
    amin, ahr, _ad, _amon, adow = _CRON_AUTO_INGEST
    sched.add_job(
        lambda: asyncio.ensure_future(run_temporal(apply=True)),
        CronTrigger(minute=tmin, hour=thr),
        id="temporal",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        lambda: asyncio.ensure_future(run_refresh()),
        CronTrigger(minute=rmin, hour=rhr, day_of_week=rdow),
        id="refresh",
        max_instances=1,
        coalesce=True,
    )
    # Automatic-ingestion jobs. max_instances=1 and coalesce=True prevent overlap and collapse missed ticks
    # into one. Both are inert until enabled (discover by discovery_enabled; auto-ingest returns early while
    # the data/AUTO_INGEST_ENABLED mirror of the operator toggle is off), so scheduling them changes nothing
    # by default. graph-changed-drain runs inside the auto-ingest cycle, so it needs no job of its own.
    sched.add_job(
        lambda: asyncio.ensure_future(run_discover()),
        CronTrigger(minute=dmin, hour=dhr),
        id="discover",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        lambda: asyncio.ensure_future(run_auto_ingest()),
        CronTrigger(minute=amin, hour=ahr, day_of_week=adow),
        id="auto-ingest",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    print(
        f"scheduler daemon up — temporal {thr}:{tmin} daily · refresh {rhr}:{rmin} dow={rdow} · "
        f"discover {dhr}:{dmin} daily · auto-ingest {ahr}:{amin} dow={adow}. Ctrl-C to stop."
    )
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown(wait=False)


async def _amain(cmd: str, *, apply: bool, do_refresh: bool, reindex: bool, live: bool) -> dict:
    try:
        if cmd == "temporal":
            return await run_temporal(apply=apply)
        if cmd == "refresh":
            return await run_refresh(reindex=reindex)
        if cmd == "daily":
            return await run_daily(apply=apply, do_refresh=do_refresh, reindex=reindex)
        if cmd == "discover":
            return await run_discover()
        if cmd == "auto-ingest":
            # --live forces a live cycle; otherwise the cycle's own auto_ingest_dry_run default applies.
            return await run_auto_ingest(dry_run=(False if live else None))
        if cmd == "graph-changed-drain":
            return await run_graph_changed_drain()
        raise SystemExit(
            f"unknown command {cmd!r} (use: temporal | refresh | daily | discover | auto-ingest | "
            f"graph-changed-drain | daemon)"
        )
    finally:
        await close_pool()


def main(argv: list[str]) -> int:
    if "--print-crontab" in argv:
        _print_crontab()
        return 0
    pos = [a for a in argv if not a.startswith("--")]
    cmd = pos[0] if pos else "daily"
    if cmd == "daemon":
        _run_daemon()
        return 0
    apply = "--apply" in argv
    do_refresh = "--no-refresh" not in argv
    reindex = "--no-reindex" not in argv
    live = "--live" in argv
    try:
        res = asyncio.run(_amain(cmd, apply=apply, do_refresh=do_refresh, reindex=reindex, live=live))
    except Exception as exc:  # noqa: BLE001
        print(f"scheduler {cmd} FAILED: {exc!r}", file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
