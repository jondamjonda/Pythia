"""Deterministic enrichment passes that bring coverage to a baseline without an LLM.

Fills the additive enrichment columns wherever rules suffice: defined_terms[], analysis roles,
deontic_modal and resolved_text (see `run`). Idempotent; no API key needed. The LLM refinement
passes (enrich_roles, enrich_definitions) can run afterwards for quality; this module is the floor.

Run (from engine/): .venv/bin/python -m pythia.ingest.enrich_parity
"""

from __future__ import annotations

import asyncio

from pythia.db import close_pool, get_pool
from pythia.graph.role_classifier import classify_roles

# Role codes obligation=2, prohibition=3, permission=4 map to a modal verb. Precedence:
# prohibition, then obligation, then permission.
_FILL_DEONTIC = """
UPDATE provision SET deontic_modal = CASE
    WHEN 3 = ANY(analysis_roles) THEN 'nesmí'
    WHEN 2 = ANY(analysis_roles) THEN 'musí'
    WHEN 4 = ANY(analysis_roles) THEN 'může' END
WHERE deontic_modal IS NULL AND analysis_roles && ARRAY[2,3,4]::smallint[]
"""

_BACKFILL_DEFINED_TERMS = """
UPDATE provision p SET defined_terms = sub.terms, is_definition = true
FROM (SELECT definition_provision_id pid, array_agg(DISTINCT term) terms
        FROM defined_term WHERE definition_provision_id IS NOT NULL GROUP BY 1) sub
WHERE p.id = sub.pid AND COALESCE(array_length(p.defined_terms, 1), 0) = 0
"""

# uses_term edges run from the using provision (src) to the defining provision (dst).
# Inline the definitions each provision uses.
_FILL_RESOLVED = """
UPDATE provision p
   SET resolved_text = p.text || E'\n\n— Definice použitých pojmů —\n' || sub.defs
FROM (SELECT e.src AS pid,
             string_agg(DISTINCT dt.term || ' — ' || left(dt.definition_text, 240), E'\n') AS defs
        FROM pythia_edge e
        JOIN defined_term dt ON dt.definition_provision_id = e.dst
       WHERE e.edge_type = 'uses_term'
       GROUP BY e.src) sub
WHERE p.id = sub.pid AND p.resolved_text IS NULL
"""


async def run() -> None:
    """Run the four passes in order and print coverage per column.

    1. defined_terms[]: backfill provision.defined_terms[] (and is_definition) from the
       defined_term table, which otherwise is barely reflected in the array.
    2. roles: cue-tag every still-untagged provision (never overwrites existing tags).
    3. deontic_modal: derive the normative modal from the obligation, prohibition and permission
       roles (a signal the GNN edge gate reads). Actor, action and condition are left to the LLM.
    4. resolved_text: inline the definitions of terms a provision uses (uses_term edges to
       defined_term) so its text is self-contained for retrieval and generation. Consumers read
       COALESCE(resolved_text, text).
    """
    pool = await get_pool()
    async with pool.acquire() as c:
        n_terms = await c.fetchval(f"WITH u AS ({_BACKFILL_DEFINED_TERMS} RETURNING 1) SELECT count(*) FROM u")
        print(f"1. defined_terms[] backfilled on {n_terms:,} provisions")
    res = await classify_roles(only_empty=True)  # fill only untagged provisions
    print(f"2. roles cue-tagged → {res['tagged']:,}/{res['total']:,} provisions now carry a role")
    async with pool.acquire() as c:
        n_de = await c.fetchval(f"WITH u AS ({_FILL_DEONTIC} RETURNING 1) SELECT count(*) FROM u")
        print(f"3. deontic_modal set on {n_de:,} provisions (from obligation/prohibition/permission roles)")
        n_rt = await c.fetchval(f"WITH u AS ({_FILL_RESOLVED} RETURNING 1) SELECT count(*) FROM u")
        print(f"4. resolved_text inlined on {n_rt:,} provisions (used-term definitions)")
        tot = await c.fetchval("SELECT count(*) FROM provision")
        print("\n=== coverage (of {:,}) ===".format(tot))
        for col, expr in [
            ("analysis_roles", "COALESCE(array_length(analysis_roles,1),0)>0"),
            ("defined_terms[]", "COALESCE(array_length(defined_terms,1),0)>0"),
            ("is_definition", "is_definition IS TRUE"),
            ("deontic_modal", "deontic_modal IS NOT NULL"),
            ("resolved_text", "resolved_text IS NOT NULL"),
        ]:
            n = await c.fetchval(f"SELECT count(*) FROM provision WHERE {expr}")
            print(f"   {col:<18} {n:>7,}  ({100 * n // tot}%)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(run())
