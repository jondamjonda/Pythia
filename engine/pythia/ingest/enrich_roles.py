"""Classify provisions by legal function with a local LLM (no API key needed).

Tags each provision with one or more legal roles (definition, obligation, prohibition, permission,
condition, exception, sanction, scope, procedure) in `provision.analysis_roles`, using the codes of
graph/role_classifier.py but more accurately than its regex cues. Roles let answer assembly order
retrieved provisions as governing duty, conditions, exceptions, sanctions and procedure.
Idempotent: already-tagged provisions are skipped.

Run (from engine/):
    .venv/bin/python -m pythia.ingest.enrich_roles [--limit N] [--acts 21/1992,370/2017] [--batch 6]
"""

from __future__ import annotations

import asyncio
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.role_classifier import ROLES
from pythia.llm import OllamaLLMAdapter

_LABEL2CODE = {v: k for k, v in ROLES.items()}
_LABELS = list(_LABEL2CODE)

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "roles": {"type": "array", "items": {"type": "string", "enum": _LABELS}},
                },
                "required": ["id", "roles"],
            },
        }
    },
    "required": ["items"],
}
_PROMPT = (
    "Klasifikuj právní FUNKCI každého z číslovaných ustanovení českého zákona. Pro každé vrať jeho "
    "'id' a seznam 'roles' (může jich platit víc) z této taxonomie:\n"
    "- definition_rule: definuje pojmy ('se rozumí')\n- obligation_rule: ukládá povinnost (musí, je povinen)\n"
    "- prohibition_rule: zakazuje (nesmí, zakazuje se)\n- permission_rule: opravňuje/dovoluje (může, je oprávněn)\n"
    "- condition_rule: stanoví podmínku (jestliže, za podmínky)\n- exception_rule: výjimka/derogace (s výjimkou, to neplatí)\n"
    "- sanction_rule: sankce/pokuta/přestupek\n- scope_rule: předmět/působnost úpravy\n"
    "- procedure_rule: postup/řízení/žádost/oznámení\n"
    "Vrať jen role, které SKUTEČNĚ platí; pokud žádná, prázdný seznam.\n\nUSTANOVENÍ:\n{block}"
)

CURRENT_NEW = ["21/1992", "6/1993", "277/2009", "370/2017", "257/2016", "374/2015", "190/2004", "15/1998", "87/1995"]


async def enrich(acts: list[str], limit: int, batch: int) -> None:
    """Tag the untagged provisions of `acts` (at most `limit`) with roles via qwen2.5 on Ollama.

    Provisions go to the model in small numbered batches of `batch` to amortise LLM cost; the result
    is written to `provision.analysis_roles` (smallint[]) as role codes.
    """
    llm = OllamaLLMAdapter()
    if not await llm.available():
        print("Ollama server not reachable — start it first.")
        return
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            """SELECT p.id::text id, sd.external_id ext, p.label, left(p.text, 380) text
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE sd.external_id = ANY($1) AND COALESCE(array_length(p.analysis_roles, 1), 0) = 0
                  AND p.text IS NOT NULL ORDER BY sd.external_id, p.ordinal LIMIT $2""",
            acts,
            limit,
        )
    print(f"classifying {len(rows)} provisions in batches of {batch} …")
    tagged = 0
    by_role: dict[str, int] = {}
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        block = "\n".join(f"[{j + 1}] {r['label']}: {' '.join(r['text'].split())}" for j, r in enumerate(chunk))
        res = None
        for _attempt in range(2):  # qwen occasionally returns empty items
            try:
                res = await llm.json(_PROMPT.format(block=block), _SCHEMA)
                if res.get("items"):
                    break
            except Exception:  # noqa: BLE001
                res = None
        if not res or not res.get("items"):
            continue
        by_id = {it["id"]: it.get("roles", []) for it in res.get("items", []) if isinstance(it.get("id"), int)}
        async with pool.acquire() as c:
            for j, r in enumerate(chunk):
                labels = [x for x in by_id.get(j + 1, []) if x in _LABEL2CODE]
                if not labels:
                    continue
                codes = sorted({_LABEL2CODE[x] for x in labels})
                await c.execute(
                    "UPDATE provision SET analysis_roles = $2::smallint[] WHERE id = $1::uuid", r["id"], codes
                )
                tagged += 1
                for x in labels:
                    by_role[x] = by_role.get(x, 0) + 1
        if (i // batch) % 10 == 0:
            print(f"  …{min(i + batch, len(rows))}/{len(rows)} provisions")
    print(f"\ntagged {tagged}/{len(rows)} provisions · by role: {dict(sorted(by_role.items(), key=lambda kv: -kv[1]))}")
    await close_pool()


if __name__ == "__main__":
    a = sys.argv[1:]
    acts = a[a.index("--acts") + 1].split(",") if "--acts" in a else CURRENT_NEW
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 100000
    bsz = int(a[a.index("--batch") + 1]) if "--batch" in a else 6
    asyncio.run(enrich(acts, limit, bsz))
