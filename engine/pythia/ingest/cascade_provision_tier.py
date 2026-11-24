"""Copy each document's legal-hierarchy tier down to its provisions.

`classify_taxonomy` puts documents on the controlled taxonomy, but `provision.legal_hierarchy` can
still hold older values (`cz-act`, `eu-regulation`, `eu-directive`), which the engine reads directly
and would pass into the `CitedProvision` contract. A provision's legal-force tier is its
instrument's tier, so this copies `source_document.legal_hierarchy` down: pure inheritance, no new
data. Dry run by default, reversible and idempotent (see `cascade`).

Run (from engine/): .venv/bin/python -m pythia.ingest.cascade_provision_tier [--apply] [--down]
(dry run by default)
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool
from pythia.graph import taxonomy as tx

_PROVTAX_V = "nt5-1"  # bump to force a re-cascade of already-normalized provisions

# Inherit the parent document's tier, back up the old value and stamp the version, only for
# provisions not yet at this version. That keeps it idempotent and makes the backup hold the original
# value rather than an already-cascaded one.
_UP = f"""
UPDATE provision p
   SET legal_hierarchy = sd.legal_hierarchy,
       metadata = jsonb_set(
                    jsonb_set(COALESCE(p.metadata, '{{}}'::jsonb),
                              '{{_legacy_provision_tier}}', to_jsonb(p.legal_hierarchy), true),
                    '{{_provtax_v}}', to_jsonb('{_PROVTAX_V}'::text), true)
  FROM source_document sd
 WHERE sd.id = p.source_document_id
   AND sd.legal_hierarchy IS NOT NULL
   AND COALESCE(p.metadata->>'_provtax_v', '') <> '{_PROVTAX_V}'
"""
_DOWN = """
UPDATE provision
   SET legal_hierarchy = COALESCE(metadata->>'_legacy_provision_tier', legal_hierarchy),
       metadata = (metadata - '_legacy_provision_tier' - '_provtax_v')
 WHERE metadata ? '_provtax_v';
"""


async def _dist(conn) -> list[tuple]:
    rows = await conn.fetch(
        "SELECT legal_hierarchy, count(*) n FROM provision GROUP BY legal_hierarchy ORDER BY n DESC"
    )
    return [(r["legal_hierarchy"], r["n"], tx.is_valid("legal_hierarchy", r["legal_hierarchy"])) for r in rows]


async def cascade(*, apply: bool = False) -> dict:
    """Report the tier distribution and, with `apply=True`, cascade document tiers to provisions.

    Precautions, as in classify_taxonomy: a dry-run preview by default; additive (the old value is
    backed up to `metadata._legacy_provision_tier`); reversible (`--down` restores it); idempotent
    (the `metadata._provtax_v` marker makes a re-run a no-op); and a post-apply conformance guard
    (any out-of-vocabulary result raises and the transaction rolls back).
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        before = await _dist(conn)
        pending = await conn.fetchval(
            f"""SELECT count(*) FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                 WHERE sd.legal_hierarchy IS NOT NULL
                   AND COALESCE(p.metadata->>'_provtax_v','') <> '{_PROVTAX_V}'"""
        )
        after = before
        if apply:
            async with conn.transaction():  # guard runs inside the transaction, so a failure rolls back
                await conn.execute(_UP)
                after = await _dist(conn)
                bad = [v for v, _, ok in after if not ok]
                if bad:
                    raise ValueError(f"tier-cascade conformance FAILED — out-of-vocab remain: {bad}")
    return {"mode": "APPLIED" if apply else "DRY-RUN (no write)", "pending": pending, "before": before, "after": after}


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DOWN)
        print("⏪ reverted: provision.legal_hierarchy restored from metadata backup.")
        await close_pool()
        return
    res = await cascade(apply=("--apply" in sys.argv))
    print(f"=== NT5 provision-grain tier cascade — {res['mode']} · {res['pending']:,} provisions pending ===")
    print("  before:")
    for v, n, ok in res["before"]:
        print(f"    {str(v):16s} {n:6d}   in_vocab={ok}")
    if res["mode"].startswith("APPLIED"):
        print("  after:")
        for v, n, ok in res["after"]:
            print(f"    {str(v):16s} {n:6d}   in_vocab={ok}")
    else:
        print("\n  (preview only — re-run with --apply to write; --down to revert)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
