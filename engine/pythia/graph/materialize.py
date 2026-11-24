"""Materialise the deterministic base layer of the legal graph ("layer A") into `pythia_edge`.

Pure SQL, idempotent and LLM-free. Edge `weight` stores prior times confidence (w0 * c); the CTE
walk and the GNN use `weight` directly, with no second confidence factor.

Sources:
  references / referenced_by          <- cross_reference WHERE ref_type='external'  (cross-statute)
  internal_reference / ..._by         <- cross_reference WHERE ref_type='internal'  (within an act, weaker)
  references (dangling)               <- cross_reference with NULL to_provision (kept as typed leaves)
  parent_of / child_of                <- provision.parent_id
  uses_term / defines                 <- defined_term matched against provision.tsv (defined_terms[] is
                                         empty), capped per term to avoid generic-term hubs
  transposes / transposed_by          <- verified transposition_map pairs, document level only

Run:  .venv/bin/python -m pythia.graph.materialize
"""

from __future__ import annotations

import asyncio
import pathlib

from pythia.db import close_pool, get_pool

_SCHEMA = (pathlib.Path(__file__).parent / "schema.sql").read_text()

# rel_id and weight literals mirror relations.R0. The weight column stores w0 * confidence.
_INSERTS: list[tuple[str, str]] = [
    (
        "references(external)",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT from_provision, to_provision, 1, 'references',
               0.9 * COALESCE(confidence, 1.0), confidence, 'cross_reference'
        FROM cross_reference
        WHERE ref_type = 'external' AND from_provision IS NOT NULL AND to_provision IS NOT NULL
          AND from_provision <> to_provision      -- no self-loops (a provision can't reference itself)
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "referenced_by(external)",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT to_provision, from_provision, 2, 'referenced_by',
               0.7 * COALESCE(confidence, 1.0), confidence, 'cross_reference'
        FROM cross_reference
        WHERE ref_type = 'external' AND from_provision IS NOT NULL AND to_provision IS NOT NULL
          AND from_provision <> to_provision      -- no self-loops (a provision can't reference itself)
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "internal_reference",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT from_provision, to_provision, 9, 'internal_reference',
               0.55 * COALESCE(confidence, 1.0), confidence, 'cross_reference'
        FROM cross_reference
        WHERE ref_type = 'internal' AND from_provision IS NOT NULL AND to_provision IS NOT NULL
          AND from_provision <> to_provision      -- no self-loops (the §-in-own-text extractor artifact)
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "internally_referenced_by",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT to_provision, from_provision, 10, 'internally_referenced_by',
               0.45 * COALESCE(confidence, 1.0), confidence, 'cross_reference'
        FROM cross_reference
        WHERE ref_type = 'internal' AND from_provision IS NOT NULL AND to_provision IS NOT NULL
          AND from_provision <> to_provision      -- no self-loops (the §-in-own-text extractor artifact)
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "references(dangling)",
        """
        INSERT INTO pythia_edge (src, dst, dst_external, rel_id, edge_type, weight, confidence, source)
        SELECT from_provision, NULL, to_external_ref, 1, 'references',
               0.9 * COALESCE(confidence, 1.0), confidence, 'cross_reference'
        FROM cross_reference
        WHERE from_provision IS NOT NULL AND to_provision IS NULL AND to_external_ref IS NOT NULL
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "parent_of",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT p.parent_id, p.id, 3, 'parent_of', 0.6, NULL, 'hierarchy'
        FROM provision p WHERE p.parent_id IS NOT NULL
          AND NOT p.is_anchor_only                                   -- B1a-03: anchors are pure
          AND p.parent_id NOT IN (SELECT id FROM provision WHERE is_anchor_only)  -- structural rows,
        ON CONFLICT DO NOTHING                                       -- never graph endpoints
    """,
    ),
    (
        "child_of",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT p.id, p.parent_id, 4, 'child_of', 0.6, NULL, 'hierarchy'
        FROM provision p WHERE p.parent_id IS NOT NULL
          AND NOT p.is_anchor_only
          AND p.parent_id NOT IN (SELECT id FROM provision WHERE is_anchor_only)
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "uses_term",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT m.pid, m.def_prov, 6, 'uses_term', 0.30, 0.6, 'defined_term'
        FROM (
            SELECT p.id AS pid, d.definition_provision_id AS def_prov,
                   count(*) OVER (PARTITION BY d.term) AS freq
            FROM defined_term d
            JOIN provision p ON p.tsv @@ plainto_tsquery('czech', d.term)
            WHERE d.definition_provision_id IS NOT NULL AND d.definition_provision_id <> p.id
        ) m
        WHERE m.freq <= 60                      -- drop generic terms (hub guard, v2 §3.6)
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "defines",
        """
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT m.def_prov, m.pid, 5, 'defines', 0.30, 0.6, 'defined_term'
        FROM (
            SELECT p.id AS pid, d.definition_provision_id AS def_prov,
                   count(*) OVER (PARTITION BY d.term) AS freq
            FROM defined_term d
            JOIN provision p ON p.tsv @@ plainto_tsquery('czech', d.term)
            WHERE d.definition_provision_id IS NOT NULL AND d.definition_provision_id <> p.id
        ) m
        WHERE m.freq <= 60
        ON CONFLICT DO NOTHING
    """,
    ),
    # Transposition is a document-level bridge only: per-article mapping is not available because
    # the Czech acts are flat. The CELEX-to-act pairs come from verified `transposition_map` rows
    # (ingest/transposition.py: SPARQL national-implementing-measure candidates, confirmed by an LLM
    # panel at over 95% precision). Each pair links its first provisions (scope or § 1 to Article 1)
    # at weight 1.0: a cross-jurisdiction anchor for the walk without a meaningless cross-product.
    (
        "transposes",
        """
        WITH pairs(cz_ext, eu_celex) AS (
            SELECT cz_external_id, eu_celex FROM transposition_map WHERE verified),
        fp AS (
            SELECT DISTINCT ON (sd.external_id) sd.external_id AS ext, p.id
            FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            ORDER BY sd.external_id, p.ordinal NULLS LAST, p.label)
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT cz.id, eu.id, 7, 'transposes', 1.0, 1.0, 'transposition'
        FROM pairs JOIN fp cz ON cz.ext = pairs.cz_ext JOIN fp eu ON eu.ext = pairs.eu_celex
        ON CONFLICT DO NOTHING
    """,
    ),
    (
        "transposed_by",
        """
        WITH pairs(cz_ext, eu_celex) AS (
            SELECT cz_external_id, eu_celex FROM transposition_map WHERE verified),
        fp AS (
            SELECT DISTINCT ON (sd.external_id) sd.external_id AS ext, p.id
            FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            ORDER BY sd.external_id, p.ordinal NULLS LAST, p.label)
        INSERT INTO pythia_edge (src, dst, rel_id, edge_type, weight, confidence, source)
        SELECT eu.id, cz.id, 8, 'transposed_by', 1.0, 1.0, 'transposition'
        FROM pairs JOIN fp cz ON cz.ext = pairs.cz_ext JOIN fp eu ON eu.ext = pairs.eu_celex
        ON CONFLICT DO NOTHING
    """,
    ),
]


async def materialize_layer_a() -> dict:
    """Rebuild the derived edges in `pythia_edge` and return edge counts by type."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA)  # asyncpg runs the whole multi-statement DDL in one call
        # Push a document's end of effect (e-Sbírka "účinnost do") down to its provisions, so the
        # current-law mask works per provision when a whole act is repealed or replaced. Idempotent.
        await conn.execute(
            """UPDATE provision p SET valid_to = sd.valid_to FROM source_document sd
               WHERE sd.id = p.source_document_id AND sd.valid_to IS NOT NULL AND p.valid_to IS NULL"""
        )
        # Delete only the edges this module derives (cross_reference, hierarchy, defined_term,
        # transposition). Edges managed elsewhere, such as the text-derived repeal edges from
        # ingest/structural_edges.py (source='nt4_repeal'), must survive, so a blanket TRUNCATE is not
        # an option. The uq_pe_edge constraint plus ON CONFLICT keep the re-insert idempotent.
        await conn.execute(
            "DELETE FROM pythia_edge WHERE source IN "
            "('cross_reference','hierarchy','defined_term','transposition') OR source IS NULL"
        )
        for _name, sql in _INSERTS:
            await conn.execute(sql)
        # Final self-loop sweep. The reference inserts already exclude src = dst, but no relation in
        # R0 is meaningfully reflexive, so remove any self-loop that a current or future insert
        # might let through instead of relying on every insert to repeat the guard.
        await conn.execute("DELETE FROM pythia_edge WHERE src = dst")
        rows = await conn.fetch("SELECT edge_type, count(*) AS n FROM pythia_edge GROUP BY edge_type ORDER BY n DESC")
        total = await conn.fetchval("SELECT count(*) FROM pythia_edge")
        src_nodes = await conn.fetchval("SELECT count(DISTINCT src) FROM pythia_edge")
        dangling = await conn.fetchval("SELECT count(*) FROM pythia_edge WHERE dst IS NULL")
    return {
        "total_edges": total,
        "src_nodes": src_nodes,
        "dangling": dangling,
        "by_type": {r["edge_type"]: r["n"] for r in rows},
    }


async def _main() -> None:
    res = await materialize_layer_a()
    print("✅ Layer-A materialized into pythia_edge")
    print(f"   total edges : {res['total_edges']:,}")
    print(f"   src nodes   : {res['src_nodes']:,}")
    print(f"   dangling    : {res['dangling']:,}")
    for t, n in res["by_type"].items():
        print(f"     {t:<26} {n:,}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
