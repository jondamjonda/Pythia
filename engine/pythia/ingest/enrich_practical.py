"""Extract verbatim practical facts (deadlines, fees, authorities, ...) into `practical_fact`.

Mines span-anchored facts from each provision's stored text into `practical_fact` (schema in
practical_fact_schema.sql), either in a deterministic regex wave (default) or in a local-LLM sweep
for deadlines the regex missed (--llm). Every row must be a verbatim slice of the provision text
(see `gate_row`). Dry run by default; sweeping the whole live corpus is a deliberate operator step,
so take a database snapshot first.

Run (from engine/):
    .venv/bin/python -m pythia.ingest.enrich_practical                 # dry-run regex plan (writes nothing)
    .venv/bin/python -m pythia.ingest.enrich_practical --apply         # regex wave, write
    .venv/bin/python -m pythia.ingest.enrich_practical --llm --apply   # LLM deadline sweep, write
    [--acts 21/1992,370/2017] [--limit N]
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass

from pythia.db import close_pool, get_pool
from pythia.ingest.extraction_coverage import record_wave
from pythia.reasoning.practical_facts import extract_practical_facts

# Extractor-version tags for provenance and safe re-runs. Bump when the extraction logic changes: a new
# version writes fresh rows alongside the old ones and never overwrites them.
REGEX_VERSION = "regex-b3p1-v1"
LLM_VERSION = "llm-lhut-v1"

# The --llm wave only sees provisions containing the deadline stem, filtered in SQL with ILIKE so the
# full corpus is never sent to the LLM.
_LLM_GATE_TOKEN = "%lhůt%"

# left(text, N) bounds the payload. The extractor and the gate both index into this same slice, so
# offsets stay consistent; a fact beyond the cap is simply not seen.
_TEXT_CAP = 8000

_FETCH_REGEX = f"""
    SELECT p.id::text id, sd.external_id ext, p.label,
           left(p.text, {_TEXT_CAP}) text
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE ($1::text[] IS NULL OR sd.external_id = ANY($1))
       AND p.text IS NOT NULL AND length(p.text) > 0
     ORDER BY sd.external_id, p.ordinal
     LIMIT $2
"""

_FETCH_LLM = f"""
    SELECT p.id::text id, sd.external_id ext, p.label,
           left(p.text, {_TEXT_CAP}) text
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE ($1::text[] IS NULL OR sd.external_id = ANY($1))
       AND p.text IS NOT NULL AND length(p.text) > 0
       AND p.text ILIKE '{_LLM_GATE_TOKEN}'
     ORDER BY sd.external_id, p.ordinal
     LIMIT $2
"""

_INSERT = """
    INSERT INTO practical_fact
        (provision_id, fact_type, value_raw, char_start, char_end, value_norm,
         extraction_method, extractor_version)
    VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7, $8)
    ON CONFLICT (provision_id, fact_type, char_start, char_end, extractor_version) DO NOTHING
"""


@dataclass(frozen=True)
class _Candidate:
    """A fact proposed for insertion, before it passes the verbatim-substring gate."""

    provision_id: str
    fact_type: str
    value_raw: str
    char_start: int
    char_end: int
    value_norm: dict | None
    extraction_method: str
    extractor_version: str


def gate_row(text: str, cand: _Candidate) -> bool:
    """Return True if the candidate's span is well formed and text[char_start:char_end] == value_raw.

    This is the core invariant for both waves and the only check that decides whether a row may be
    written: every stored fact is a verbatim slice of the law. A failing candidate is counted and
    dropped, never repaired. Pure; no I/O.
    """
    s, e = cand.char_start, cand.char_end
    if s < 0 or e <= s or e > len(text):
        return False
    return text[s:e] == cand.value_raw


# ---- regex wave -------------------------------------------------------------------------------------------


def regex_candidates(provision_id: str, text: str) -> list[_Candidate]:
    """Run the pure extractor and wrap each fact as a regex-wave candidate.

    Uses `reasoning/practical_facts.extract_practical_facts` (deadline, authority, penalty, form,
    channel, fee, procedure_step; whatever types it returns are written, so new types flow through
    automatically). Deterministic, cheap, no API key; tagged extractor_version=REGEX_VERSION. The
    extractor already guarantees the substring identity; the gate re-checks it independently.
    """
    out: list[_Candidate] = []
    for f in extract_practical_facts(text):
        out.append(
            _Candidate(
                provision_id=provision_id,
                fact_type=f.fact_type,
                value_raw=f.value_raw,
                char_start=f.char_start,
                char_end=f.char_end,
                value_norm=None,
                extraction_method="regex",
                extractor_version=REGEX_VERSION,
            )
        )
    return out


# ---- --llm lhůt* wave -------------------------------------------------------------------------------------

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "deadlines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"char_start": {"type": "integer"}, "char_end": {"type": "integer"}},
                "required": ["char_start", "char_end"],
            },
        }
    },
    "required": ["deadlines"],
}

_LLM_PROMPT = (
    "Níže je text ustanovení českého právního předpisu. Najdi všechny LHŮTY (časové limity — "
    "např. 'do 30 dnů', 've lhůtě 15 dnů', 'nejpozději do konce měsíce'). Pro každou lhůtu vrať POUZE "
    "znakové offsety 'char_start' a 'char_end' tak, aby text[char_start:char_end] byl PŘESNĚ ten úsek "
    "textu, který lhůtu vyjadřuje. NEPŘEPISUJ text, nevracej samotný řetězec — jen offsety. Pokud text "
    "žádnou lhůtu neobsahuje, vrať prázdný seznam.\n\nTEXT (offsety počítej od 0):\n{text}"
)


async def llm_candidates(llm, provision_id: str, text: str) -> list[_Candidate]:
    """Ask the local LLM for deadline spans in `text` and return them as candidates.

    A separate, cheap sweep for deadline clauses the regex missed, tagged
    extractor_version=LLM_VERSION. Only provisions containing "lhůt" (the stem of lhůta, "time
    limit") are sent, and the regex types are not re-run. The model proposes character offsets only;
    value_raw is sliced from our own text, so a string echoed by the model is never trusted, and a
    malformed span is dropped rather than patched. Any LLM error returns an empty list, so the
    provision is simply skipped.
    """
    try:
        res = await llm.json(_LLM_PROMPT.format(text=text), _LLM_SCHEMA, stage="enrich_practical")
    except Exception:  # noqa: BLE001
        return []
    out: list[_Candidate] = []
    for d in res.get("deadlines") or []:
        try:
            s = int(d["char_start"])
            e = int(d["char_end"])
        except (KeyError, TypeError, ValueError):
            continue
        if s < 0 or e <= s or e > len(text):
            continue  # malformed span: drop (the gate would reject it anyway)
        raw = text[s:e]  # value_raw is our own slice, never the model's echo
        if not raw.strip():
            continue
        out.append(
            _Candidate(
                provision_id=provision_id,
                fact_type="deadline",
                value_raw=raw,
                char_start=s,
                char_end=e,
                value_norm=None,
                extraction_method="llm",
                extractor_version=LLM_VERSION,
            )
        )
    return out


# ---- driver -----------------------------------------------------------------------------------------------


async def _write(pool, cands: list[_Candidate]) -> int:
    written = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            for c in cands:
                norm = json.dumps(c.value_norm) if c.value_norm is not None else None
                await conn.execute(
                    _INSERT,
                    c.provision_id,
                    c.fact_type,
                    c.value_raw,
                    c.char_start,
                    c.char_end,
                    norm,
                    c.extraction_method,
                    c.extractor_version,
                )
                written += 1
    return written


async def run(acts: list[str] | None, limit: int, apply: bool, use_llm: bool) -> dict:
    """Run one extraction wave and return a summary of what was found and written.

    With apply=False (the CLI default) candidates are computed and gated but nothing is written.
    With apply=True rows are inserted with ON CONFLICT (provision_id, fact_type, char_start,
    char_end, extractor_version) DO NOTHING, so re-running the same version is a no-op. Only
    `practical_fact` and the extraction_coverage manifest are written; `provision` is never
    modified. The summary has the keys wave, apply, provisions, candidates, accepted, rejected,
    written, by_type, acts_scanned and coverage_rows_written.
    """
    pool = await get_pool()
    llm = None
    if use_llm:
        from pythia.llm import OllamaLLMAdapter

        llm = OllamaLLMAdapter()
        if not await llm.available():
            print("Ollama server not reachable — start it first (or drop --llm).")
            await close_pool()
            return {"wave": "llm", "error": "ollama_unreachable"}

    async with pool.acquire() as conn:
        rows = await conn.fetch(_FETCH_LLM if use_llm else _FETCH_REGEX, acts, limit)

    accepted: list[_Candidate] = []
    rejected = 0
    candidates = 0
    by_type: dict[str, int] = {}
    # Count scanned provisions per act so the coverage manifest can record which acts this wave ran over
    # (fact_lookup uses it to tell extraction_not_run from no_fact_row). `id2ext` maps a candidate's
    # provision id back to its act to attribute written facts per act.
    acts_scanned: dict[str, int] = {}
    id2ext: dict[str, str] = {}
    for r in rows:
        text = r["text"]
        ext = r["ext"]
        if ext:
            acts_scanned[ext] = acts_scanned.get(ext, 0) + 1
            id2ext[r["id"]] = ext
        cands = (await llm_candidates(llm, r["id"], text)) if use_llm else regex_candidates(r["id"], text)
        candidates += len(cands)
        for c in cands:
            if gate_row(text, c):  # the single verbatim-substring check
                accepted.append(c)
                by_type[c.fact_type] = by_type.get(c.fact_type, 0) + 1
            else:
                rejected += 1  # dropped, never repaired

    written = 0
    coverage_written = 0
    if apply and accepted:
        written = await _write(pool, accepted)
    # With --apply, record coverage for every act this wave scanned, including acts that yielded no
    # facts: a scanned act without facts must read as no_fact_row, not extraction_not_run. record_wave
    # is a no-op when the extraction_coverage table is absent, so this never breaks a run. A dry run
    # writes nothing.
    if apply and acts_scanned:
        version = LLM_VERSION if use_llm else REGEX_VERSION
        wave_name = "llm" if use_llm else "regex"
        facts_by_act: dict[str, int] = {}
        for c in accepted:
            e = id2ext.get(c.provision_id)
            if e:
                facts_by_act[e] = facts_by_act.get(e, 0) + 1
        async with pool.acquire() as conn:
            for ext, scanned in acts_scanned.items():
                if await record_wave(
                    conn, ext, wave_name, version, provisions_scanned=scanned, facts_written=facts_by_act.get(ext, 0)
                ):
                    coverage_written += 1

    await close_pool()
    return {
        "wave": "llm" if use_llm else "regex",
        "apply": apply,
        "provisions": len(rows),
        "candidates": candidates,
        "accepted": len(accepted),
        "rejected": rejected,
        "written": written,
        "by_type": by_type,
        "acts_scanned": len(acts_scanned),
        "coverage_rows_written": coverage_written,
    }


def _print_plan(res: dict) -> None:
    print(
        f"=== enrich_practical [{res.get('wave')}] {'APPLY' if res.get('apply') else 'DRY-RUN (nothing written)'} ==="
    )
    if res.get("error"):
        print(f"  ERROR: {res['error']}")
        return
    print(f"  provisions scanned : {res['provisions']}")
    print(f"  candidates         : {res['candidates']}")
    print(f"  accepted (gate ok) : {res['accepted']}")
    print(f"  rejected (non-substr/malformed): {res['rejected']}")
    print(f"  written            : {res['written']}")
    print(f"  by_type            : {json.dumps(res['by_type'], ensure_ascii=False)}")


if __name__ == "__main__":
    a = sys.argv[1:]
    acts = a[a.index("--acts") + 1].split(",") if "--acts" in a else None
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 100000
    apply = "--apply" in a
    use_llm = "--llm" in a
    res = asyncio.run(run(acts, limit, apply, use_llm))
    _print_plan(res)
    sys.exit(0)
