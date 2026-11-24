"""Static analysis-role tagging: the legal function of each provision (definition, obligation, ...).

The provision.deontic_* and is_definition columns are empty in this corpus, so roles cannot be read
from columns. They are derived from Czech and English cue phrases in the text instead, which are
present (549 obligation, 256 prohibition and 22 definition cues). This deterministic cue engine is
also the basis for the normative/non-normative split. Roles are approximate; the target is at least
0.85 F1 against lawyer labels.

Writes the additive column provision.analysis_roles smallint[].
Run:  .venv/bin/python -m pythia.graph.role_classifier
"""

from __future__ import annotations

import asyncio

from pythia.db import close_pool, get_pool

ROLES: dict[int, str] = {
    1: "definition_rule",
    2: "obligation_rule",
    3: "prohibition_rule",
    4: "permission_rule",
    5: "condition_rule",
    6: "exception_rule",
    7: "sanction_rule",
    8: "scope_rule",
    9: "procedure_rule",
}

_DDL = """
ALTER TABLE provision ADD COLUMN IF NOT EXISTS analysis_roles smallint[] DEFAULT '{}';
CREATE INDEX IF NOT EXISTS ix_p_roles ON provision USING gin (analysis_roles);
"""

# Czech and English cue regexes per role (case-insensitive, \y = word boundary). Conservative:
# unmatched provisions get no role. A provision may carry several roles (set semantics).
_CLASSIFY = r"""
UPDATE provision SET analysis_roles = ARRAY_REMOVE(ARRAY[
  CASE WHEN text ~* '(se rozumí|pro účely tohoto|\ymeans\y|shall mean)'                 THEN 1 END,
  CASE WHEN text ~* '(je povinen|je povinna|jsou povinni|\ymusí\y|\yshall\y)'           THEN 2 END,
  CASE WHEN text ~* '(nesmí|zakazuje se|není dovoleno|shall not|is prohibited|may not)' THEN 3 END,
  CASE WHEN text ~* '(je oprávněn|\ylze\y|\ysmí\y|\ymůže\y|\ymay\y)'                     THEN 4 END,
  CASE WHEN text ~* '(jestliže|za podmínky|pouze pokud|provided that|where the)'        THEN 5 END,
  CASE WHEN text ~* '(s výjimkou|nevztahuje se|to neplatí|by way of derogation|except)' THEN 6 END,
  CASE WHEN text ~* '(za přestupek|dopustí se přestupku|uloží pokut|penalt|sanction)'   THEN 7 END,
  CASE WHEN text ~* '(tento zákon upravuje|předmět úpravy|this (regulation|directive))' THEN 8 END,
  CASE WHEN text ~* '(podá se žádost|rozhodne o|oznámí|shall (submit|notify)|application for)' THEN 9 END
]::smallint[], NULL)
"""


async def classify_roles(only_empty: bool = True) -> dict:
    """Tag provision.analysis_roles from text cues and return per-role counts.

    With `only_empty=True` (the default) only untagged provisions are filled, so better tags (for
    example LLM-refined ones) are never overwritten and the run is idempotent.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_DDL)
        sql = _CLASSIFY + (" WHERE COALESCE(array_length(analysis_roles, 1), 0) = 0" if only_empty else "")
        await conn.execute(sql)
        rows = await conn.fetch(
            "SELECT r AS role, count(*) AS n FROM provision, unnest(analysis_roles) r GROUP BY r ORDER BY r"
        )
        tagged = await conn.fetchval("SELECT count(*) FROM provision WHERE array_length(analysis_roles, 1) > 0")
        total = await conn.fetchval("SELECT count(*) FROM provision")
    return {"tagged": tagged, "total": total, "by_role": {ROLES[r["role"]]: r["n"] for r in rows}}


async def _main() -> None:
    res = await classify_roles()
    print(f"✅ analysis_roles tagged {res['tagged']:,}/{res['total']:,} provisions")
    for role, n in res["by_role"].items():
        print(f"     {role:<18} {n:,}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
