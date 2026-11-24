"""Generate synthetic stage-2 training data that teaches the GNN to follow the question.

Stage 1 teaches graph structure but not query relevance, so on its own the GNN ignores the question
(its rankings are dominated by hub provisions and it beats no baseline). Stage 2 fixes that with
supervised (query, gold) examples, generated here without a paid API:

- pick a real anchor provision that has structural neighbours;
- gold = the anchor plus its real neighbours (resolved citations in both directions, hierarchy,
  definitions and case-law edges);
- query = a natural question the local LLM (qwen2.5) writes from the anchor text, in its language;
- seeds = what the sparse seeder returns for that question, matching inference.

Appends rows {anchor, anchor_label, lang, query, seed_ids, gold_ids} to train/stage2_data.jsonl.
Resumable: anchors already in the file are skipped.

Run (from engine/, after stage-1 training has finished, so the two do not compete for the GPU):
    .venv/bin/python train/gen_stage2.py [n]
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.relations import TRAVERSABLE_CASE_LAW_REL_IDS
from pythia.graph.seed import sparse_seed
from pythia.llm import OllamaLLMAdapter

_OUT = pathlib.Path(__file__).parent / "stage2_data.jsonl"

# Case-law neighbours (for anchors and gold) come from the materialised graph (pythia_edge), using
# the traversable case-law relations in both directions (src = $1 or dst = $1). Reading pythia_edge
# rather than cross_reference / parent_id, which know nothing about case law, means any relation
# family added to the graph later is covered automatically. The relation ids come from the registry,
# never hard-coded. $2 is the array of traversable case-law rel_ids.
_ANCHORS = """
SELECT p.id::text id, p.label, p.text, sd.language lang
FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
WHERE length(p.text) BETWEEN 120 AND 2200
  AND sd.external_id NOT IN ('89/2012','90/2012')   -- anchors = regulatory corpus; the general codes stay GOLD targets when cited
  AND ( EXISTS (SELECT 1 FROM cross_reference cr WHERE cr.from_provision=p.id AND cr.to_provision IS NOT NULL AND cr.to_provision<>p.id)
        OR EXISTS (SELECT 1 FROM cross_reference cr WHERE cr.to_provision=p.id AND cr.from_provision<>p.id)
        OR EXISTS (SELECT 1 FROM provision c WHERE c.parent_id=p.id)
        -- case-law-aware: a provision engaged by a ruling via a traversable construal edge is an anchor too.
        OR EXISTS (SELECT 1 FROM pythia_edge e WHERE e.dst IS NOT NULL AND e.rel_id = ANY($2::int[])
                   AND ((e.src=p.id AND e.dst<>p.id) OR (e.dst=p.id AND e.src<>p.id))) )
ORDER BY md5(p.id::text) LIMIT $1
"""
_GOLD = """
SELECT DISTINCT n.pid::text id FROM (
    SELECT to_provision pid FROM cross_reference WHERE from_provision=$1 AND to_provision IS NOT NULL
    UNION SELECT from_provision FROM cross_reference WHERE to_provision=$1 AND from_provision IS NOT NULL
    UNION SELECT id FROM provision WHERE parent_id=$1
    UNION SELECT parent_id FROM provision WHERE id=$1 AND parent_id IS NOT NULL
    -- case-law construal neighbours from the materialized graph, both directions (rulings⇄provisions).
    UNION SELECT dst FROM pythia_edge WHERE src=$1 AND dst IS NOT NULL AND rel_id = ANY($2::int[])
    UNION SELECT src FROM pythia_edge WHERE dst=$1 AND rel_id = ANY($2::int[])
) n WHERE n.pid IS NOT NULL AND n.pid <> $1 LIMIT 7
"""

# Case-law rel_ids, used for verification and debugging: if one of these links a row's gold
# neighbour, that provision was reached only through a case-law edge.
_CASE_LAW_REL_IDS = list(TRAVERSABLE_CASE_LAW_REL_IDS)

_PROMPT = {
    "cs": (
        "Jsi český právník se specializací na finanční regulaci. Níže je ustanovení zákona. "
        "Napiš JEDNU přirozenou, konkrétní otázku (česky), kterou by položil compliance "
        "specialista a kterou toto ustanovení spolu se souvisejícími ustanoveními zodpovídá. "
        "Vrať pouze otázku, bez uvození.\n\nUstanovení {label}:\n{text}"
    ),
    "en": (
        "You are an EU financial-regulation lawyer. Below is a legal provision. Write ONE "
        "natural, specific question (English) a compliance officer would ask that this "
        "provision together with related provisions answers. Return ONLY the question.\n\n"
        "Provision {label}:\n{text}"
    ),
}


def _clean(q: str) -> str:
    q = q.strip().strip('"').strip()
    q = re.sub(r"^(otázka|question)\s*[:\-]\s*", "", q, flags=re.IGNORECASE)
    return q.split("\n")[0].strip()


async def main(n: int = 250) -> None:
    done = set()
    if _OUT.exists():
        for ln in _OUT.read_text().splitlines():
            if ln.strip():
                done.add(json.loads(ln)["anchor"])
    llm = OllamaLLMAdapter()
    if not await llm.available():
        print("Ollama server not reachable — start it first.")
        return
    pool = await get_pool()
    written = skipped = 0
    async with pool.acquire() as conn:
        anchors = await conn.fetch(_ANCHORS, n, _CASE_LAW_REL_IDS)
        with _OUT.open("a") as f:
            for i, a in enumerate(anchors, 1):
                if a["id"] in done:
                    continue
                gold = [r["id"] for r in await conn.fetch(_GOLD, a["id"], _CASE_LAW_REL_IDS)]
                if len(gold) < 1:
                    skipped += 1
                    continue
                lang = a["lang"] if a["lang"] in _PROMPT else "en"
                try:
                    q = _clean(await llm.text(_PROMPT[lang].format(label=a["label"], text=a["text"][:1200])))
                except Exception as exc:  # noqa: BLE001
                    print(f"  LLM fail on {a['label']}: {repr(exc)[:50]}")
                    skipped += 1
                    continue
                if len(q) < 12:
                    skipped += 1
                    continue
                seeds = [s.provision_id for s in await sparse_seed(q, k=6)]
                if not seeds:
                    skipped += 1
                    continue
                f.write(
                    json.dumps(
                        {
                            "anchor": a["id"],
                            "anchor_label": a["label"],
                            "lang": lang,
                            "query": q,
                            "seed_ids": seeds,
                            "gold_ids": [a["id"]] + gold,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                f.flush()
                written += 1
                if written % 25 == 0:
                    print(f"  {written} written (skipped {skipped}) … last [{a['label']}] q={q[:60]!r}")
    print(f"DONE · wrote {written}, skipped {skipped} → {_OUT.name} (total {written + len(done)})")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 250))
