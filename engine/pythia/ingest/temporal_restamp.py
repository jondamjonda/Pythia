"""Re-stamp source_document.temporal_status so it reflects the current date.

temporal_status is computed once at ingest and then frozen in the column, so it goes stale as dates
pass: a document stamped NOT_YET_EFFECTIVE becomes IN_FORCE on its valid_from, and an IN_FORCE act past
its valid_to becomes REPEALED. This job recomputes it for every document (document level only) with the
rule ingest uses. Re-running at the same as_of changes nothing. Dry run by default (prints the grouped
plan); --apply writes.

Run (from engine/):
  .venv/bin/python -m pythia.ingest.temporal_restamp                 # dry run: print the plan only
  .venv/bin/python -m pythia.ingest.temporal_restamp --as-of 2026-07-02
  .venv/bin/python -m pythia.ingest.temporal_restamp --apply         # print the plan, apply, print the count
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date

from pythia.db import close_pool, get_pool
from pythia.graph.taxonomy import _temporal_status  # the single source of truth; do not re-implement


async def plan_restamp(session, *, as_of: date | None = None) -> list[dict]:
    """Return the source_document rows whose stored temporal_status differs from the recomputed one.

    The status is recomputed with ``taxonomy._temporal_status(valid_from, valid_to, as_of)``, the
    function ``taxonomy.classify`` calls at ingest (with as_of set to the ingest day), so the boundary
    rules live in one place. Left stale, core acts such as 455/1991 Sb. (Trade Licensing) or 187/2006 Sb.
    (Sickness Insurance) would keep reading NOT_YET_EFFECTIVE after they take effect. This is separate
    from ``provision_versioning.recompute_temporal_status`` because that function only touches documents
    with a provenance bundle; this covers every row. The status is fully recomputable from valid_from and
    valid_to, so this is a derived-value refresh.

    Read-only. `as_of` defaults to today; the CLI passes it explicitly, and tests pass it to pin a
    boundary date. `session` is any object with an async ``fetch(sql)`` (an asyncpg connection in
    production, a mock in tests).

    Returns ``{id, ref, valid_from, valid_to, old_status, new_status}`` dicts for changed rows only.
    """
    ref = as_of or date.today()
    rows = await session.fetch(
        "SELECT id, external_id, title, valid_from, valid_to, temporal_status FROM source_document"
    )
    changes: list[dict] = []
    for r in rows:
        old = r["temporal_status"]
        new = _temporal_status(r["valid_from"], r["valid_to"], ref)
        if old != new:
            changes.append(
                {
                    "id": r["id"],
                    "ref": r["external_id"] or r["title"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                    "old_status": old,
                    "new_status": new,
                }
            )
    return changes


async def apply_restamp(session, changes) -> int:
    """Write the recomputed status for each planned change and return the number of rows updated.

    Idempotent: afterwards ``plan_restamp`` at the same as_of returns an empty list, so re-applying
    changes nothing. `session` must provide an async ``execute(sql, *args)``.
    """
    n = 0
    for ch in changes:
        await session.execute(
            "UPDATE source_document SET temporal_status = $2 WHERE id = $1",
            ch["id"],
            ch["new_status"],
        )
        n += 1
    return n


def _format_plan(changes: list[dict], *, ref: date) -> str:
    """Render the dry-run report, grouped by status transition (old to new) in a deterministic order."""
    if not changes:
        return f"temporal re-stamp plan (as_of={ref.isoformat()}): 0 changes — corpus already current."
    groups: dict[str, list[str]] = defaultdict(list)
    for ch in changes:
        groups[f"{ch['old_status']}→{ch['new_status']}"].append(str(ch["ref"]))
    lines = [f"temporal re-stamp plan (as_of={ref.isoformat()}): {len(changes)} doc(s) would change"]
    for transition in sorted(groups):
        refs = groups[transition]
        lines.append(f"  {transition}: {len(refs)} doc(s)  [{', '.join(sorted(refs))}]")
    lines.append(f"  TOTAL: {len(changes)} doc(s)")
    return "\n".join(lines)


async def _main() -> None:
    ap = argparse.ArgumentParser(
        description="AQ-04 doc-level temporal_status re-stamp (default = DRY-RUN, writes nothing)."
    )
    ap.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="reference date for the status rule (default: today)",
    )
    ap.add_argument(
        "--apply", action="store_true", help="write the recomputed statuses (default: DRY-RUN — print the plan only)"
    )
    a = ap.parse_args()
    ref = a.as_of or date.today()

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            changes = await plan_restamp(conn, as_of=ref)
            print(_format_plan(changes, ref=ref), flush=True)
            # The provision table has no temporal_status column; its temporal facet is the separate
            # `pit_status` (from the head provision_version window), maintained by
            # provision_versioning.recompute_provision_temporal_status. There is nothing to re-stamp at
            # provision level, so say so explicitly.
            print(
                "provision-level: `provision` has no temporal_status column (its facet is the distinct "
                "`pit_status`); AQ-04 is doc-level only — nothing to re-stamp at provision grain.",
                flush=True,
            )
            if a.apply:
                async with conn.transaction():
                    n = await apply_restamp(conn, changes)
                print(f"APPLIED · updated {n} row(s).", flush=True)
            elif changes:
                print("\n(dry-run only — re-run with --apply to write.)", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
