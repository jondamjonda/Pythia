"""Section-kind classifier for EU articles: the EU_SECONDARY adapter of the section-kind standard.

The Czech classifier (`section_kind`) votes a kind from the § children of Část/Hlava/Díl anchors,
but EU acts have no such anchors: their hierarchy is a flat `art_N` / `art_N.para_M` tree. In EU law
the article itself is the section-bearing unit and names its kind in its italic title
(`## Article 4` then `*Definitions*`; `## Article 99` then `*Entry into force and application*`),
so the kind is read straight from that title using the shared marker table
(`section_kind._KIND_MARKERS`). Only the title finder differs per legal family; the result feeds
the same `trust_resolve.TIER_MAP`.

`section_kind` is written on the real article row (is_anchor_only=false); nothing else couples the
two columns. The trust walk-up in `ports._decorate` starts with the provision's own section_kind, so
an article resolves immediately and its paragraphs inherit it one hop up. EU regulations are
HARD_LAW_ERGA_OMNES, so `trust_resolve` floors binding EU provisions at hard law even when a title
is missed: a miss loses precision, never safety. A title with no marker stays `operative`. Runs are
idempotent (`only_empty=True` never overwrites a human `kind_override`), need no reindex, and
`--unmake` clears only rows whose kind_source carries the `b1a08eu` prefix.

  dry-run :  .venv/bin/python -m pythia.graph.section_kind_eu
  apply   :  .venv/bin/python -m pythia.graph.section_kind_eu --backfill
  undo    :  .venv/bin/python -m pythia.graph.section_kind_eu --unmake   (alias: --drop, --down)
"""

from __future__ import annotations

import asyncio
import re
import sys

from pythia.db import close_pool, get_pool

# Shared kind vocabulary and marker table; this adapter only swaps the title finder.
from pythia.graph.section_kind import (
    _KIND_MARKERS,
    KIND_OPERATIVE,
    SECTION_KINDS,
    ensure_schema,
)

# Provenance prefix on every row this pass writes. --unmake keys on it, so it reverts only these
# rows and never a Czech anchor tag or a human kind_override.
_KIND_SOURCE_PREFIX = "b1a08eu"

# The EU article title: the `*italic*` heading on the line(s) right after the "## Article N" header,
# as in the corpus ("## Article 4\n\n*Definitions*\n\nFor the purposes of this Regulation:").
_EU_TITLE = re.compile(r"##\s*Article\s+[^\n*]*\n+\s*\*([^*\n]+)\*", re.IGNORECASE)
# Fallback: the first short `*italic*` span near the top of the chunk (for acts with different
# spacing).
_FALLBACK_ITALIC = re.compile(r"\*([^*\n]{2,90})\*")


def eu_title(text: str | None) -> str:
    """Return the article's own title from its body markdown, or "" if it has none."""
    if not text:
        return ""
    m = _EU_TITLE.search(text)
    if m:
        return m.group(1).strip()
    m2 = _FALLBACK_ITALIC.search(text[:240])  # only the top, never an emphasis span in the body
    return m2.group(1).strip() if m2 else ""


def classify_eu_title(title: str) -> str:
    """Return the section_kind for an EU article from its own title, defaulting to operative.

    Hyphens are normalised to spaces for matching ('Subject-matter' becomes 'Subject matter') so the
    English phrase markers ('subject matter', 'entry into force', ...) fire on hyphenated EU
    headings. A title that matches no marker stays `operative`, the neutral binding default.
    """
    if not title:
        return KIND_OPERATIVE
    probe = title.replace("-", " ")
    for kind, pat in _KIND_MARKERS:
        if pat.search(probe):
            return kind
    return KIND_OPERATIVE


def plan_eu(rows: list[dict]) -> list[dict]:
    """Plan section kinds for EU article rows (pure, no database).

    `rows` are `{id, text}` dicts. Returns one `{id, section_kind, kind_source}` per article;
    kind_source records the title that was read, for auditing.
    """
    out: list[dict] = []
    for r in rows:
        title = eu_title(r.get("text"))
        kind = classify_eu_title(title)
        out.append(
            {
                "id": r["id"],
                "section_kind": kind,
                "kind_source": f"{_KIND_SOURCE_PREFIX} title={title[:60]!r}",
            }
        )
    return out


# Each EU article is a depth-1 ltree node (`art_N`, no parent); its paragraphs are depth-2 children
# that inherit through the parent_id walk-up. So only articles are classified (nlevel = 1).
# Single-paragraph articles without children are depth 1 too, so they are covered and carry their
# own tag.
_FETCH_EU_ARTICLES = """
    SELECT p.id::text AS id, p.text
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE sd.legal_hierarchy = 'EU_SECONDARY'
       AND p.is_anchor_only = false
       AND nlevel(p.hierarchy_path) = 1
       AND ($1::boolean = false OR p.section_kind IS NULL)   -- only_empty: never clobber an existing tag
"""


async def classify_eu(*, apply: bool = False, only_empty: bool = True) -> dict:
    """Tag every EU article's section_kind from its own title.

    Writes section_kind and kind_source only when `apply` is true. With `only_empty=True` (the
    default) already-tagged rows are skipped, so the run is idempotent and a human kind_override is
    preserved.
    """
    await ensure_schema()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_FETCH_EU_ARTICLES, only_empty)]
        plans = plan_eu(rows)
        totals = {"articles": len(plans), "written": 0, "by_kind": {k: 0 for k in SECTION_KINDS}}
        for p in plans:
            totals["by_kind"][p["section_kind"]] += 1
        if apply and plans:
            async with conn.transaction():
                await conn.executemany(
                    "UPDATE provision SET section_kind = $2, kind_source = $3 "
                    "WHERE id = $1::uuid AND is_anchor_only = false"
                    + ("" if not only_empty else " AND section_kind IS NULL"),
                    [(p["id"], p["section_kind"], p["kind_source"]) for p in plans],
                )
            totals["written"] = len(plans)  # executemany applies every row; count the planned rows
    totals["mode"] = "APPLIED" if apply else "DRY-RUN (no write)"
    return totals


async def unmake() -> dict:
    """Clear section_kind and kind_source on the rows this pass wrote.

    Rows are selected by the b1a08eu provenance prefix, so a Czech anchor tag or a human
    kind_override is never touched.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "WITH u AS (UPDATE provision SET section_kind = NULL, kind_source = NULL "
            "WHERE kind_source LIKE $1 RETURNING 1) SELECT count(*) FROM u",
            f"{_KIND_SOURCE_PREFIX}%",
        )
    return {"reverted": n}


async def _main() -> None:
    if any(f in sys.argv for f in ("--unmake", "--drop", "--down")):
        res = await unmake()
        print(f"⏪ reverted: NULLed section_kind on {res['reverted']:,} EU Article rows (b1a08eu prefix).")
        await close_pool()
        return
    res = await classify_eu(apply=("--backfill" in sys.argv))
    print(f"=== EU Article section-kind classifier — {res['mode']} ===")
    print(f"  EU articles classified : {res['articles']:,}")
    for k in SECTION_KINDS:
        print(f"     {k:<20} {res['by_kind'][k]:,}")
    if res["mode"].startswith("DRY"):
        print("\n  (preview only — re-run with --backfill to write; --unmake to revert)")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
