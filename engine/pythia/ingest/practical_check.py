"""Integrity check for the `practical_fact` table, run after an enrich_practical wave.

Re-asserts over the live table the invariants the insert-time gate should already guarantee, so a
bad row can never sit undetected: substring integrity, well-formed spans, no orphan rows, and known
fact types and extraction methods (see `check`). Per-type counts are reported, not asserted, so the
operator sees the shape of the extraction. Read-only.

Run (from engine/): .venv/bin/python -m pythia.ingest.practical_check  (prints PASS/FAIL, exits 0 or 1)
"""

from __future__ import annotations

import asyncio
import json
import sys

from pythia.db import close_pool, get_pool

_VALID_FACT_TYPES = ("deadline", "authority", "penalty", "form", "channel", "fee", "procedure_step", "amount")
_VALID_METHODS = ("regex", "llm")

# The substring check runs in SQL, as substr(p.text, char_start + 1, char_end - char_start), so that
# tens of thousands of texts are not pulled into Python (Postgres substr is 1-indexed).
#
# Rows whose recorded span does not slice back to value_raw. The extractor reads at most the first
# 8000 characters, but this indexes the full provision.text so an out-of-range end shows up as a
# mismatch instead of being silently truncated.
_SUBSTRING_VIOLATIONS = """
    SELECT count(*) FROM practical_fact pf
      JOIN provision p ON p.id = pf.provision_id
     WHERE substr(p.text, pf.char_start + 1, pf.char_end - pf.char_start) IS DISTINCT FROM pf.value_raw
"""

_ORPHAN_ROWS = """
    SELECT count(*) FROM practical_fact pf
     WHERE NOT EXISTS (SELECT 1 FROM provision p WHERE p.id = pf.provision_id)
"""

_BAD_SPANS = """
    SELECT count(*) FROM practical_fact
     WHERE char_start < 0 OR char_end <= char_start
"""

_BAD_TYPES = """
    SELECT count(*) FROM practical_fact WHERE fact_type <> ALL($1::text[])
"""

_BAD_METHODS = """
    SELECT count(*) FROM practical_fact WHERE extraction_method <> ALL($1::text[])
"""

_PER_TYPE = """
    SELECT fact_type, extraction_method, count(*) n
      FROM practical_fact GROUP BY fact_type, extraction_method ORDER BY fact_type, extraction_method
"""


async def check() -> dict:
    """Run the practical_fact invariants and return per-check results plus detail counts.

    Substring integrity (provision.text[char_start:char_end] == value_raw for every row) is the
    core grounding invariant: every fact is a verbatim slice of the law, and one violation fails.
    Well-formed spans (char_start >= 0 and char_end > char_start) are defence in depth behind the
    table's CHECK constraint. fact_type and extraction_method must come from the canonical sets.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM practical_fact")
        substr_bad = await conn.fetchval(_SUBSTRING_VIOLATIONS)
        orphans = await conn.fetchval(_ORPHAN_ROWS)
        bad_spans = await conn.fetchval(_BAD_SPANS)
        bad_types = await conn.fetchval(_BAD_TYPES, list(_VALID_FACT_TYPES))
        bad_methods = await conn.fetchval(_BAD_METHODS, list(_VALID_METHODS))
        per_type = await conn.fetch(_PER_TYPE)
    counts = {f"{r['fact_type']}/{r['extraction_method']}": r["n"] for r in per_type}
    checks = {
        "100% substring integrity (violations = 0)": substr_bad == 0,
        "no orphan rows (provision missing = 0)": orphans == 0,
        "well-formed spans (bad = 0)": bad_spans == 0,
        "known fact_type (bad = 0)": bad_types == 0,
        "known extraction_method (bad = 0)": bad_methods == 0,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "detail": {
            "total_rows": total,
            "substring_violations": substr_bad,
            "orphan_rows": orphans,
            "bad_spans": bad_spans,
            "bad_types": bad_types,
            "bad_methods": bad_methods,
            "per_type": counts,
        },
    }


async def _main() -> None:
    res = await check()
    print("=== practical_fact invariant harness ===")
    for name, ok in res["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print("\n  counts:", json.dumps(res["detail"]["per_type"], ensure_ascii=False))
    if not res["ok"]:
        print("  detail:", json.dumps(res["detail"], ensure_ascii=False, default=str))
    print(f"\n  {'✅ ALL INVARIANTS HOLD' if res['ok'] else '❌ INVARIANT VIOLATION'}")
    await close_pool()
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    asyncio.run(_main())
