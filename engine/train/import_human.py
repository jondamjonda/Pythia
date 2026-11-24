"""Import human-written training examples into the stage-2 pipeline.

You write questions and their gold provisions (the ones a lawyer would actually cite) in the same
readable format as the evaluation set: each provision as "<external_id> <label>", for example
"240/2013 § 297". This tool:

- resolves each gold reference against the database;
- reports every reference it cannot find, so mistyped labels can be fixed;
- derives the seeds from the question with the sparse seeder, unless the row has a "seeds" list;
- appends ready-to-train rows to train/stage2_data.jsonl, which finetune_stage2.py reads.

Input rows (train/human_train.jsonl by default), one JSON object per line:
  {"id":"aif-mktg-01","query":"…","jurisdiction":["CZ","EU"],
   "gold":["240/2013 § 297","240/2013 § 298"], "gold_hops":2, "difficulty":"medium", "note":"…"}
  Add "seeds":["240/2013 § 296"] to override the derived seeds.

Run (from engine/):
  import:       .venv/bin/python train/import_human.py [human_file.jsonl]
  find labels:  .venv/bin/python train/import_human.py --find 240/2013 297
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

from pythia.db import close_pool, get_pool
from pythia.graph.seed import sparse_seed

_OUT = pathlib.Path(__file__).parent / "stage2_data.jsonl"
_DEFAULT = pathlib.Path(__file__).parent / "human_train.jsonl"


async def _resolve(conn, ref: str) -> str | None:
    ext, _, label = ref.partition(" ")
    return await conn.fetchval(
        "SELECT p.id::text FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
        "WHERE sd.external_id=$1 AND p.label=$2",
        ext,
        label.strip(),
    )


async def find(ext: str, sub: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT p.label FROM provision p JOIN source_document sd ON sd.id=p.source_document_id "
            "WHERE sd.external_id=$1 AND p.label ILIKE '%'||$2||'%' ORDER BY p.ordinal LIMIT 40",
            ext,
            sub,
        )
    print(
        f"{ext} labels matching {sub!r}: "
        + (
            ", ".join(repr(r["label"]) for r in rows)
            if rows
            else "NONE (check the external_id and try a shorter substring)"
        )
    )
    await close_pool()


async def main(path: str) -> None:
    p = pathlib.Path(path)
    if not p.exists():
        print(f"no file {p} — create it (see the Training Material Guide)")
        return
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    pool = await get_pool()
    ok = skipped = 0
    unresolved: list[str] = []
    with _OUT.open("a") as f:
        async with pool.acquire() as c:
            for it in rows:
                gold_ids: list[str] = []
                for ref in it.get("gold", []):
                    pid = await _resolve(c, ref)
                    gold_ids.append(pid) if pid else unresolved.append(ref)
                if not gold_ids:
                    print(f"  skip {it.get('id', it.get('query', '')[:40])!r}: no gold resolved")
                    skipped += 1
                    continue
                if it.get("seeds"):
                    seeds = []
                    for ref in it["seeds"]:
                        pid = await _resolve(c, ref)
                        if pid:
                            seeds.append(pid)
                else:
                    seeds = [
                        s.provision_id for s in await sparse_seed(it["query"], k=8, jurisdiction=it.get("jurisdiction"))
                    ]
                if not seeds:
                    print(f'  skip {it.get("id", "")!r}: no seeds (question has no lexical match; add a "seeds" list)')
                    skipped += 1
                    continue
                f.write(
                    json.dumps(
                        {
                            "anchor": it.get("id", gold_ids[0]),
                            "anchor_label": it.get("id", ""),
                            "lang": "human",
                            "query": it["query"],
                            "seed_ids": seeds,
                            "gold_ids": gold_ids,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                ok += 1
    print(f"\nimported {ok} examples (skipped {skipped}) → {_OUT.name}")
    if unresolved:
        uniq = list(dict.fromkeys(unresolved))
        print(f"⚠ {len(uniq)} gold reference(s) NOT FOUND — fix the label (use --find):")
        for u in uniq[:25]:
            print(f"    {u!r}")
    await close_pool()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--find":
        asyncio.run(find(sys.argv[2], sys.argv[3]))
    else:
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else str(_DEFAULT)))
