"""Link statute empowerment provisions to the implementing decrees present in the corpus.

`structural_edges.py` only marks empowerment provisions (zmocňovací ustanovení, which delegate
rule-making to a decree) with the boolean `provision.delegates_decree`; it writes no edges. Once a
decree is ingested, this module adds a `delegates_authority` edge (relation 11) from the statute
provision to the decree and the inverse `implements` edge (relation 12), so graph expansion can
reach the implementing decree from the empowering §. Dry run by default: it prints the edges only.

Run (from engine/): .venv/bin/python -m pythia.ingest.decree_edges [--apply]
"""

from __future__ import annotations

import asyncio

from pythia.graph.relations import BY_LABEL
from pythia.ingest.decree_queue import extract_decree_refs

_SOURCE = "b3p2_delegation"  # pythia_edge.source tag; isolates these edges for an idempotent rebuild
_DELEGATES = BY_LABEL["delegates_authority"]  # relation 11: statute § to decree
_IMPLEMENTS = BY_LABEL["implements"]  # relation 12: decree to statute §


def resolve_delegation_edges(
    *,
    delegating_provisions: list[tuple[str, str]],
    decree_anchors: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Match delegating provisions to decrees in the corpus; a pure function with no database access.

    A provision is linked to a decree when it is marked as an empowerment (`delegates_decree =
    TRUE`), its text cites the decree by number ("vyhláška č. 296/2017", "nařízení vlády č.
    361/2007") as found by `extract_decree_refs`, and a decree with that external_id is in the
    corpus. The link runs to the decree's anchor provision (its first by ordinal), the same
    act-anchor convention as structural_edges._build_repeals.

    Args:
        delegating_provisions: (provision_id, text) for each provision with delegates_decree = TRUE.
        decree_anchors: decree external_id to its anchor provision id, for decrees in the corpus.

    Returns:
        De-duplicated (delegating_provision_id, decree_anchor_provision_id, decree_external_id)
        links in a deterministic order. Self-links are dropped. The caller writes each link as a
        forward relation-11 edge plus an inverse relation-12 edge.
    """
    edges: set[tuple[str, str, str]] = set()
    for prov_id, text in delegating_provisions:
        for ext, _kind in extract_decree_refs(text or ""):
            dst = decree_anchors.get(ext)
            if dst and dst != prov_id:
                edges.add((prov_id, dst, ext))
    # Deterministic order: decree external_id, then head id, then tail id.
    return sorted(edges, key=lambda e: (e[2], e[0], e[1]))


def build_edge_rows(edges: list[tuple[str, str, str]]) -> list[tuple]:
    """Expand each resolved link into a forward relation-11 row and an inverse relation-12 row.

    Rows have the same shape as in structural_edges._build_repeals:
    (src, dst, rel_id, edge_type, weight, confidence, source, metadata_json). Confidence is 1.0
    because the link is structural: a marked empowerment provision that names a decree in the corpus.
    """
    import json

    rows: list[tuple] = []
    for src, dst, ext in edges:
        meta = json.dumps({"decree_external_id": ext}, ensure_ascii=False)
        rows.append((src, dst, _DELEGATES.rel_id, "delegates_authority", _DELEGATES.default_weight, 1.0, _SOURCE, meta))
        rows.append((dst, src, _IMPLEMENTS.rel_id, "implements", _IMPLEMENTS.default_weight, 1.0, _SOURCE, meta))
    return rows


# ── database driver ──────────────────────────────────────────────────────────────────────────────────
async def _fetch_inputs(conn) -> tuple[list, dict]:
    """Read the delegating provisions and the anchor provision of every decree in the corpus.

    A decree is a source_document whose document_type is an implementing regulation (vyhláška or
    nařízení vlády). Decrees are keyed by external_id, the same "num/year" form citations use.
    """
    delegating = [
        (str(r["id"]), r["text"])
        for r in await conn.fetch("SELECT id, text FROM provision WHERE delegates_decree IS TRUE")
    ]
    decree_anchors: dict[str, str] = {}
    for r in await conn.fetch(
        """SELECT sd.external_id AS ext,
                  (SELECT id FROM provision WHERE source_document_id = sd.id
                    ORDER BY ordinal LIMIT 1) AS anchor
             FROM source_document sd
            WHERE sd.document_type IN ('vyhlaska','vyhlaska_cnb','narizeni_vlady')
              AND sd.external_id ~ '^\\d+/\\d{4}$'"""
    ):
        if r["anchor"] is not None:
            decree_anchors[r["ext"]] = str(r["anchor"])
    return delegating, decree_anchors


async def resolve(*, apply: bool = False) -> dict:
    """Resolve the delegation edges and, with `apply`, replace the stored ones; dry run by default.

    A dry run resolves against the real corpus but writes nothing. Applying is idempotent: every
    edge with this module's source tag is deleted and reinserted in one transaction, and the unique
    index uq_pe_edge turns duplicate inserts into no-ops.
    """
    from pythia.db import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        delegating, decree_anchors = await _fetch_inputs(conn)
        edges = resolve_delegation_edges(delegating_provisions=delegating, decree_anchors=decree_anchors)
        if apply:
            rows = build_edge_rows(edges)
            async with conn.transaction():
                await conn.execute("DELETE FROM pythia_edge WHERE source = $1", _SOURCE)
                await conn.executemany(
                    """INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source, metadata)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT DO NOTHING""",
                    rows,
                )
    return {
        "mode": "APPLIED" if apply else "DRY-RUN (no write)",
        "delegating_provisions": len(delegating),
        "decrees_in_corpus": len(decree_anchors),
        "resolved_seats": len(edges),
        "edges_written": len(edges) * 2 if apply else 0,
        "preview": [{"src": s, "dst": d, "decree": ext} for s, d, ext in edges[:20]],
    }


async def _main() -> None:
    res = await resolve(apply=("--apply" in __import__("sys").argv))
    print(f"=== B3-P2 delegation-edge resolver — {res['mode']} ===")
    print(f"  delegating provisions (delegates_decree): {res['delegating_provisions']}")
    print(f"  decrees present in corpus               : {res['decrees_in_corpus']}")
    print(
        f"  resolved statute→decree seats           : {res['resolved_seats']} "
        f"(×2 with rel-12 inverse → {res['resolved_seats'] * 2} edges)"
    )
    for e in res["preview"]:
        print(f"    {e['src'][:8]}… →[delegates_authority]→ č.{e['decree']} ({e['dst'][:8]}…)")
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — --apply is a separate human-approved step; this unit never ran it)")


if __name__ == "__main__":
    asyncio.run(_main())
