"""Structural edges derivable from consolidated statute text: delegation markers and repeals.

Of the structural relations in the registry (`relations.py`, rel_ids 11 to 20), two can be built
and resolved from consolidated statutes alone: delegation of authority (flagging empowerment
provisions) and repeals (`repeals` / `repealed_by` edges). `amends`, `consolidates_into` and
`supersedes` are registered but not populated, since the corpus has no amendment (novela) or
version lineage yet. Dry-run preview by default; additive, idempotent and reversible with `--down`.

Run (from engine/): .venv/bin/python -m pythia.ingest.structural_edges [--apply] [--down]
"""

from __future__ import annotations

import asyncio
import re
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.relations import BY_LABEL

# Empowerment clause (zmocňovací ustanovení): an empowering verb followed closely by a decree or
# implementing-regulation object. Conservative: excludes bare "podle prováděcího předpisu"
# references that merely use an implementing regulation.
_DELEGATE_CUE = (
    r"(stanoví|vydá|upraví|zmocňuje se)\M.{0,55}"
    r"(vyhlášk|nařízením vlády|prováděc\w*\s*(právní\s*)?předpis)"
)
# Repeal clause (zrušovací ustanovení).
_REPEAL_CUE = r"(zrušuje se|se zrušuj|zrušují se)"
_ACT_REF = re.compile(r"č\.\s*(\d+/\d{4})")  # "č. 591/1992" -> external_id "591/1992"

_DDL = """
ALTER TABLE provision ADD COLUMN IF NOT EXISTS delegates_decree boolean DEFAULT false;
CREATE INDEX IF NOT EXISTS ix_p_delegates ON provision (delegates_decree) WHERE delegates_decree;
"""
_DOWN = """
DELETE FROM pythia_edge WHERE source = 'nt4_repeal';
ALTER TABLE provision DROP COLUMN IF EXISTS delegates_decree;
"""


async def _mark_delegating(conn, apply: bool) -> int:
    """Flag empowerment provisions in provision.delegates_decree and return how many match.

    Empowerment provisions (zmocňovací ustanovení) grant decree-making power; the flag is a node
    signal. The statute-to-decree edge itself is resolved when decrees are ingested; flagging the
    delegating provisions now makes that resolution cheap. This is the most valuable structural
    signal in the financial corpus. Recomputed on every run, so idempotent.
    """
    if apply:
        await conn.execute(_DDL)
        # Only write rows whose flag would actually change (IS DISTINCT FROM), so a reindex with no
        # text changes rewrites almost nothing instead of every provision. Same result, much faster.
        await conn.execute(
            "UPDATE provision SET delegates_decree = (text ~* $1) WHERE delegates_decree IS DISTINCT FROM (text ~* $1)",
            _DELEGATE_CUE,
        )
    return await conn.fetchval("SELECT count(*) FROM provision WHERE text ~* $1", _DELEGATE_CUE)


async def _build_repeals(conn, apply: bool) -> dict:
    """Link repeal clauses (zrušovací ustanovení) to the in-corpus acts they repeal.

    The targets are mostly crawled, already-repealed predecessor acts. With `apply`, the
    source='nt4_repeal' edges are deleted and reinserted, so re-runs are idempotent.
    """
    rows = await conn.fetch("SELECT id, text FROM provision WHERE text ~* $1", _REPEAL_CUE)
    # Resolve only to acts whose status is REPEALED. A still-in-force act named after the repeal verb
    # is being amended, not repealed, so this cleanly separates genuine repeals from the amendment
    # clauses that follow the repeal list.
    anchors = {
        r["ext"]: r["aid"]
        for r in await conn.fetch(
            """SELECT sd.external_id ext,
                  (SELECT id FROM provision WHERE source_document_id = sd.id ORDER BY ordinal LIMIT 1) aid
             FROM source_document sd WHERE sd.temporal_status = 'REPEALED'"""
        )
    }
    edges, refs_seen, refs_resolved = set(), 0, 0
    cue_re = re.compile(_REPEAL_CUE, re.IGNORECASE)
    for r in rows:
        # Only the text from the repeal verb onwards is the repeal list; acts named earlier in the
        # provision appear in another context (an amendment or a plain reference).
        m_cue = cue_re.search(r["text"])
        tail = r["text"][m_cue.start() :] if m_cue else ""
        for m in _ACT_REF.finditer(tail):
            refs_seen += 1
            dst = anchors.get(m.group(1))
            if dst and dst != r["id"]:
                refs_resolved += 1
                edges.add((r["id"], dst))
    if apply:
        await conn.execute("DELETE FROM pythia_edge WHERE source = 'nt4_repeal'")
        rep, repby = BY_LABEL["repeals"], BY_LABEL["repealed_by"]
        fwd = [(s, d, rep.rel_id, "repeals", rep.default_weight, "nt4_repeal") for s, d in edges]
        inv = [(d, s, repby.rel_id, "repealed_by", repby.default_weight, "nt4_repeal") for s, d in edges]
        await conn.executemany(
            """INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, source)
               VALUES ($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING""",
            fwd + inv,
        )
    return {
        "repeal_clauses": len(rows),
        "act_refs": refs_seen,
        "resolved_to_corpus": refs_resolved,
        "edges": len(edges),
    }


async def apply_structural(*, apply: bool = False) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        delegating = await _mark_delegating(conn, apply)
        repeals = await _build_repeals(conn, apply)
    return {
        "mode": "APPLIED" if apply else "DRY-RUN (no write)",
        "delegating_provisions": delegating,
        "repeals": repeals,
    }


async def _main() -> None:
    if "--down" in sys.argv:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DOWN)
        print("⏪ reverted: nt4_repeal edges deleted; provision.delegates_decree dropped.")
        await close_pool()
        return
    res = await apply_structural(apply=("--apply" in sys.argv))
    rp = res["repeals"]
    print(f"=== NT4 structural edges — {res['mode']} ===")
    print(
        f"  delegates_authority (foundation): {res['delegating_provisions']} delegating provisions "
        f"marked (zmocňovací ustanovení) — statute→decree edges resolve when decrees arrive (N4)"
    )
    print(
        f"  repeals: {rp['repeal_clauses']} repeal clauses · {rp['act_refs']} act refs · "
        f"{rp['resolved_to_corpus']} resolved in-corpus → {rp['edges']} repeals edges (×2 with inverse)"
    )
    print("  deferred (no source data): amends / consolidates_into / supersedes — registered, not populated")
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --apply to write)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
