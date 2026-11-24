"""Extract defined terms from Czech and English provisions into `defined_term` with a local LLM.

A local LLM (qwen2.5 via Ollama, no API key needed) extracts (term, definition) pairs from each
definition-bearing provision, Czech or English, so `materialize_layer_a` can build `defines` and
`uses_term` edges. This is the single authoritative definition extractor for the served corpus:
extend the SQL or prompt here rather than also running the web app's OpenAI extractor over these
acts. Run it after any re-ingest of an act, which deletes the act's definitions (see `enrich`).

Run (from engine/):
    .venv/bin/python -m pythia.ingest.enrich_definitions [--limit N] [--acts 21/1992,370/2017] [--rematerialize]
    .venv/bin/python -m pythia.ingest.enrich_definitions --all-missing [--limit N] [--rematerialize]
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool
from pythia.llm import OllamaLLMAdapter

_SCHEMA = {
    "type": "object",
    "properties": {
        "definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"term": {"type": "string"}, "definition": {"type": "string"}},
                "required": ["term", "definition"],
            },
        }
    },
    "required": ["definitions"],
}
_PROMPT = (
    "Z následujícího ustanovení českého zákona vyextrahuj VŠECHNY pojmy, které ustanovení definuje. "
    "Pojmy jsou uvozeny tvarem '… se rozumí' a bývají v 7. pádě (např. 'pojišťovnou', 'bankou'). "
    "Pro každý definovaný pojem vrať 'term' v ZÁKLADNÍM tvaru (1. pád jednotného čísla, např. "
    "'pojišťovna', 'banka') a stručnou 'definition' (jedna věta). Vrať pouze pojmy, které toto "
    "ustanovení skutečně definuje; pokud žádné nedefinuje, vrať prázdný seznam.\n\nUSTANOVENÍ:\n{text}"
)

# English prompt for the EU acts, with the same JSON schema. EU drafting style is
# "'<term>' means <definition>" or "for the purposes of this Regulation, <term> means ...".
_PROMPT_EN = (
    "From the following provision of an EU legal act, extract ALL terms that the provision DEFINES. "
    'Definitions use markers such as "\'X\' means …", "X shall mean …", "for the purposes of this '
    'Regulation/Directive …", or "the following definitions apply". For each defined term return '
    "'term' as the bare defined word or phrase in its base form (singular, no surrounding quotes, "
    "e.g. 'credit institution', 'beneficial owner') and a concise one-sentence 'definition'. Return "
    "ONLY terms this provision actually defines; if it defines none, return an empty list."
    "\n\nPROVISION:\n{text}"
)

# Czech definition markers: "se rozumí" ("is understood as") and "pro účely tohoto zákona" ("for the
# purposes of this act"); the Czech prompt asks for the term in the nominative singular. They match
# rows in any language because the corpus was originally Czech only; changing them would change the
# Czech candidate set. English markers are separate.
_CZ_MARKERS = "p.text ILIKE '%se rozumí%' OR p.text ILIKE '%pro účely tohoto zákona%'"
# English definition markers, chosen by measuring the EU acts in the corpus: 'means' and 'the
# following definitions' occur in all of them, 'for the purposes of this' in nearly all, 'shall
# mean' in a few. Restricted to language='en' (see _CANDIDATE_PREDICATE) so they never widen the
# Czech candidate set.
_EN_MARKERS = (
    "p.text ILIKE '%means%' OR p.text ILIKE '%shall mean%' "
    "OR p.text ILIKE '%for the purposes of this%' OR p.text ILIKE '%the following definitions%'"
)

# Candidate predicate: Czech markers on any row, or English markers on language='en' rows only.
# Because the English branch requires sd.language='en', the Czech ('cs') rows selected are exactly
# those `_CZ_MARKERS` alone would select.
_CANDIDATE_PREDICATE = f"(({_CZ_MARKERS}) OR (sd.language = 'en' AND ({_EN_MARKERS})))"

# Default act list: in-force Czech financial acts.
CURRENT_NEW = ["21/1992", "6/1993", "277/2009", "370/2017", "257/2016", "374/2015", "190/2004", "15/1998", "87/1995"]

# Acts (excluding case law, since rulings do not define terms) that still have at least one
# candidate provision with no linked definition. Checked per provision, so acts that already hold
# some defined_term rows are still returned if any candidate is unenriched; the per-provision fetch
# below then handles exactly those provisions. Resolved at runtime for --all-missing, so newly
# ingested acts are covered automatically.
_MISSING_PROVISION_ACTS = f"""
    SELECT DISTINCT sd.external_id
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE sd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
       AND ({_CANDIDATE_PREDICATE})
       AND NOT EXISTS (SELECT 1 FROM defined_term dt WHERE dt.definition_provision_id = p.id)
     ORDER BY sd.external_id"""

# Candidate provisions to enrich for the resolved act list: the same predicate plus the
# per-provision `NOT EXISTS (definition_provision_id)` resume guard. Selects sd.language so the loop
# can choose the Czech or English prompt per row.
_CANDIDATE_PROVISIONS = f"""
    SELECT p.id::text id, sd.id::text doc, sd.external_id ext, sd.language lang,
           p.label, left(p.text, 2500) text
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE sd.external_id = ANY($1)
       AND ({_CANDIDATE_PREDICATE})
       AND NOT EXISTS (SELECT 1 FROM defined_term dt WHERE dt.definition_provision_id = p.id)
     ORDER BY sd.external_id, p.ordinal LIMIT $2"""


def _prompt_for(lang: str | None) -> str:
    """Return the extraction prompt for the row's language (English for EU acts, otherwise Czech)."""
    return _PROMPT_EN if (lang or "").lower() == "en" else _PROMPT


async def enrich(acts: list[str], limit: int, rematerialize: bool, all_missing: bool = False) -> None:
    """Extract definitions from the candidate provisions of `acts` with the local LLM.

    Inserts (term, definition) rows into `defined_term` and sets provision.is_definition and
    defined_terms[]; with `rematerialize`, rebuilds the graph edges afterwards.

    With `all_missing`, `acts` (by default the built-in CURRENT_NEW list) is replaced by every act
    except case law (rulings do not define terms) that still has an unenriched candidate
    provision. The pass is slow, so it is bounded by `limit` (total provisions across all acts);
    the per-provision `NOT EXISTS (definition_provision_id)` guard makes it resumable, so
    re-running continues where the previous run stopped.

    Ordering matters: re-ingesting an act deletes its provisions (esbirka._delete_existing), and
    `defined_term.definition_provision_id` is a foreign key with ON DELETE CASCADE (see
    regsuper/storage/models.py), so re-ingesting an enriched act wipes its definitions. Run this
    after any re-ingest.
    """
    llm = OllamaLLMAdapter()
    if not await llm.available():
        print("Ollama server not reachable — start it first.")
        return
    pool = await get_pool()
    if all_missing:
        async with pool.acquire() as c:
            acts = [r["external_id"] for r in await c.fetch(_MISSING_PROVISION_ACTS)]
        print(f"--all-missing: {len(acts)} non-CASE_LAW acts have un-enriched candidate provisions")
        if not acts:
            print("nothing to enrich — every candidate provision already has definitions.")
            await close_pool()
            return
    async with pool.acquire() as c:
        rows = await c.fetch(_CANDIDATE_PROVISIONS, acts, limit)
    n_en = sum(1 for r in rows if (r["lang"] or "").lower() == "en")
    print(f"enriching {len(rows)} definition provisions with the local LLM ({len(rows) - n_en} cz / {n_en} en) …")
    total = 0
    for r in rows:
        try:
            res = await llm.json(_prompt_for(r["lang"]).format(text=r["text"]), _SCHEMA)
            defs = [
                (d["term"].strip(), d["definition"].strip())
                for d in res.get("definitions", [])
                if d.get("term") and d.get("definition")
            ]
        except Exception as exc:  # noqa: BLE001
            print(f"  {r['ext']} {r['label']}: LLM error {repr(exc)[:50]}")
            continue
        if not defs:
            continue
        async with pool.acquire() as c:
            async with c.transaction():
                for term, definition in defs:
                    await c.execute(
                        """INSERT INTO defined_term
                               (term, scope_document_id, definition_provision_id, definition_text,
                                extracted_by, confidence, created_at)
                           VALUES ($1, $2::uuid, $3::uuid, $4, 'qwen2.5:14b', 0.8, now())""",
                        term,
                        r["doc"],
                        r["id"],
                        definition,
                    )
                await c.execute(
                    "UPDATE provision SET is_definition = true, defined_terms = $2 WHERE id = $1::uuid",
                    r["id"],
                    [t for t, _ in defs],
                )
        total += len(defs)
        print(f"  {r['ext']} {r['label']}: +{len(defs)} terms (e.g. {defs[0][0]!r})")
    print(f"\ninserted {total} defined terms across {len(rows)} provisions")
    if rematerialize and total:
        from pythia.graph.materialize import materialize_layer_a

        print(f"re-materialized → {(await materialize_layer_a())['total_edges']:,} edges")
    await close_pool()


if __name__ == "__main__":
    a = sys.argv[1:]
    all_missing = "--all-missing" in a
    acts = a[a.index("--acts") + 1].split(",") if "--acts" in a else CURRENT_NEW
    # --all-missing resolves its own act list from the database and sweeps the whole corpus, so its
    # default cap is higher. It is still bounded; re-run to continue past the cap.
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else (8000 if all_missing else 1000)
    asyncio.run(enrich(acts, limit, "--rematerialize" in a, all_missing=all_missing))
