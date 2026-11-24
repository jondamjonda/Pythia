"""Resolve dangling intra-act citations to real provisions.

The legacy ingestion left internal `cross_reference` rows (ref_type='internal') whose only target
is a locator string such as "art_10__para_1" or "par_297.odst_3.pism_a". Both the GNN and the CTE
expansion skip edges without a destination, so these are citations the engine cannot follow,
nearly all of them section-to-section links inside Czech statutes. This maps each locator to the
finest node modelled in the same document. No LLM; idempotent; reversible with --unresolve.

Run (from engine/):
  dry run :  .venv/bin/python -m pythia.ingest.resolve_internal --dry-run
  apply   :  .venv/bin/python -m pythia.ingest.resolve_internal
  undo    :  .venv/bin/python -m pythia.ingest.resolve_internal --unresolve
"""

from __future__ import annotations

import argparse
import asyncio
import re

from pythia.db import close_pool, get_pool

_VERIFIER = "pythia-internal-resolver"
CONF_FINE = 0.90  # matched to the exact cited paragraph/article (EU "Article N(M)" / "Article N")
CONF_COARSE = 0.75  # matched only to the section (§); Czech paragraph detail is not modelled

_LOC_EU = re.compile(r"art_(\d+[a-z]?)(?:__para_(\d+))?", re.IGNORECASE)  # art_10__para_1
_LOC_CZ = re.compile(r"par_(\d+[a-z]?)", re.IGNORECASE)  # par_297(.odst_…)


def candidate_labels(locator: str) -> tuple[list[str], float]:
    """Turn a locator into candidate target labels (finest first) and a confidence for the match."""
    if locator.lower().startswith("art_"):
        m = _LOC_EU.match(locator)
        if m:
            art, para = m.group(1), m.group(2)
            if para:
                return [f"Article {art}({para})", f"Article {art}"], CONF_FINE
            return [f"Article {art}"], CONF_FINE
    m = _LOC_CZ.match(locator)
    if m:
        return [f"§ {m.group(1)}"], CONF_COARSE  # section level; odst./písm. detail is not modelled
    return [], 0.0


async def _resolve(conn, from_doc, labels: list[str]):
    """Return the first (finest) label that exists as a provision in the same document, with its id."""
    for lab in labels:
        pid = await conn.fetchval(
            "SELECT id FROM provision WHERE source_document_id = $1 AND label = $2 LIMIT 1", from_doc, lab
        )
        if pid is not None:
            return pid, lab
    return None, None


async def resolve_internal_xrefs(*, dry_run: bool = False, rematerialize: bool = True) -> dict:
    """Link dangling internal cross-references to provisions and return per-outcome counts.

    Rows have `to_provision` NULL and a locator in `to_external_ref`. They are inert rather than
    harmful (tensors.py requires dst IS NOT NULL), but lose coverage. Each locator maps to the
    finest node actually modelled, within the same document:

        EU   "art_10__para_1"          -> "Article 10(1)" if it exists, else "Article 10"
        EU   "art_112"                 -> "Article 112"
        CZ   "par_297.odst_3.pism_a"   -> "§ 297"  (paragraphs are not modelled; § is the finest level)
        CZ   "par_2a.odst_1"           -> "§ 2a"

    Only `to_provision`, `confidence` and verified_by='pythia-internal-resolver' are written; then,
    if `rematerialize` and not `dry_run`, `materialize_layer_a()` turns the rows into
    internal_reference / internally_referenced_by edges. Losing the paragraph and point
    granularity is a data-modelling limit (Czech acts are stored at section level), not something
    this resolver can fix.
    """
    pool = await get_pool()
    stats = {
        "candidates": 0,
        "linked_fine": 0,
        "linked_coarse": 0,
        "skipped_no_node": 0,
        "skipped_unparseable": 0,
        "self": 0,
    }
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT cr.id, cr.to_external_ref, cr.from_provision, p.source_document_id AS from_doc
               FROM cross_reference cr JOIN provision p ON p.id = cr.from_provision
               WHERE cr.ref_type = 'internal' AND cr.to_provision IS NULL
                 AND cr.to_external_ref IS NOT NULL AND cr.from_provision IS NOT NULL"""
        )
        stats["candidates"] = len(rows)
        for r in rows:
            labels, conf = candidate_labels(r["to_external_ref"])
            if not labels:
                stats["skipped_unparseable"] += 1
                continue
            pid, hit_label = await _resolve(conn, r["from_doc"], labels)
            if pid is None:
                stats["skipped_no_node"] += 1
                continue
            if pid == r["from_provision"]:
                stats["self"] += 1
                continue
            fine = conf == CONF_FINE and hit_label == labels[0]  # matched the finest candidate
            stats["linked_fine" if fine else "linked_coarse"] += 1
            if not dry_run:
                await conn.execute(
                    """UPDATE cross_reference SET to_provision = $1, confidence = $2, verified_by = $3
                       WHERE id = $4""",
                    pid,
                    conf if fine else CONF_COARSE,
                    _VERIFIER,
                    r["id"],
                )
    stats["linked_total"] = stats["linked_fine"] + stats["linked_coarse"]
    if not dry_run and rematerialize and stats["linked_total"]:
        from pythia.graph.materialize import materialize_layer_a

        stats["graph_edges"] = (await materialize_layer_a())["total_edges"]
    return stats


async def unresolve() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """WITH u AS (UPDATE cross_reference SET to_provision = NULL, verified_by = NULL
                          WHERE verified_by = $1 RETURNING 1) SELECT count(*) FROM u""",
            _VERIFIER,
        )
    from pythia.graph.materialize import materialize_layer_a

    await materialize_layer_a()
    return n


def _main() -> None:
    ap = argparse.ArgumentParser(description="Resolve dangling intra-act citations to real provisions.")
    ap.add_argument("--dry-run", action="store_true", help="report what would link, write nothing")
    ap.add_argument("--unresolve", action="store_true", help="undo all links made by this resolver")
    a = ap.parse_args()

    async def run():
        if a.unresolve:
            print(f"unresolved {await unresolve()} rows; graph re-materialized")
        else:
            s = await resolve_internal_xrefs(dry_run=a.dry_run)
            tag = "DRY-RUN (nothing written)" if a.dry_run else "APPLIED"
            print(f"=== intra-act citation cleanup — {tag} ===")
            print(f"  dangling internal candidates : {s['candidates']}")
            print(f"  linked at exact grain (EU §M) : {s['linked_fine']}")
            print(f"  linked at § floor (CZ)        : {s['linked_coarse']}")
            print(f"  linked TOTAL                  : {s['linked_total']}")
            print(f"  skipped (no such node)        : {s['skipped_no_node']}")
            print(f"  skipped (unparseable locator) : {s['skipped_unparseable']}")
            print(f"  skipped (self-reference)      : {s['self']}")
            if "graph_edges" in s:
                print(f"  graph re-materialized → {s['graph_edges']:,} edges")
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
