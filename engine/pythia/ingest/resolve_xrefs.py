"""Resolve dangling cross-act citations to real provisions.

Cross-statute citations are stored as `cross_reference` rows (ref_type='external') whose
`to_provision` is NULL: only the target act's name is known (e.g. "Directive 2014/65" or "zákona
č. 89/2012 Sb."), not which node is cited. This links each row to the cited article of the target
act, or else to the act's anchor provision, whenever the target act is in the corpus. No LLM;
reversible with --unresolve.

Run (from engine/):
  dry run :  .venv/bin/python -m pythia.ingest.resolve_xrefs --dry-run
  apply   :  .venv/bin/python -m pythia.ingest.resolve_xrefs            (then re-materialises)
  undo    :  .venv/bin/python -m pythia.ingest.resolve_xrefs --unresolve
"""

from __future__ import annotations

import argparse
import asyncio
import re
from dataclasses import dataclass

from pythia.db import close_pool, get_pool

_VERIFIER = "pythia-xref-resolver"
CONF_ARTICLE = 0.85  # precise: linked to the exact cited article/§
CONF_ACT = 0.50  # coarse: linked to the target act's anchor provision

_EU_TYPE = re.compile(r"\b(Directive|Regulation|Decision)\b", re.IGNORECASE)
_EU_LETTER = {"directive": "L", "regulation": "R", "decision": "D"}
_CZ_NUM = re.compile(r"č\.\s*(\d+/\d+)\s*Sb\.", re.IGNORECASE)  # "č. 89/2012 Sb." -> 89/2012
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


@dataclass
class Target:
    external_id: str  # the document to look up (CELEX or "89/2012")
    num_token: str  # the number/year token as written, for searching the citing text
    kind: str  # 'eu' | 'cz'


def parse_target(ext_ref: str) -> Target | None:
    """Turn a target-act name into the corpus document id and a token to search the citing text for."""
    if not ext_ref:
        return None
    # Czech act: "zákona č. 89/2012 Sb." or "vyhláška č. 194/2011 Sb."
    cz = _CZ_NUM.search(ext_ref)
    if cz:
        num = cz.group(1)  # "89/2012" is already number/year, i.e. the external_id
        return Target(external_id=num, num_token=num, kind="cz")
    # EU instrument: a type word followed by two numbers around a slash
    t = _EU_TYPE.search(ext_ref)
    if t:
        nums = re.findall(r"\d{1,4}", ext_ref[t.end() :])
        if len(nums) >= 2:
            a, b = nums[0], nums[1]
            year = a if _YEAR.match(a) else (b if _YEAR.match(b) else None)
            if year:
                seq = b if year == a else a
                celex = f"3{year}{_EU_LETTER[t.group(1).lower()]}{int(seq):04d}"
                return Target(external_id=celex, num_token=f"{a}/{b}", kind="eu")
    return None


def find_article(citing_text: str, tgt: Target) -> str | None:
    """Return the article or section number cited for this act, or None.

    Looks just before each mention of the act's number token for "Article N" (EU) or "§ N" /
    "Čl. N" (CZ), keeping the closest match.

    The "Čl. N" / "Článek N" form matters for the constitutional acts (the Constitution 1/1993,
    the Charter of Fundamental Rights 2/1993 and other constitutional laws), which are divided into
    articles (články) rather than sections. A citation such as "čl. 1 odst. 1 Listiny ...
    (č. 2/1993 Sb.)" names its target only as "Čl. N"; without this branch the resolver would fall
    back to the act's anchor at the coarse CONF_ACT confidence.
    """
    if not citing_text:
        return None
    head = re.compile(
        r"Article\s+(\d+[a-z]?)" if tgt.kind == "eu" else r"(?:§|Čl\.|Článek)\s*(\d+[a-z]?)", re.IGNORECASE
    )
    best: str | None = None
    for m in re.finditer(re.escape(tgt.num_token), citing_text):
        window = citing_text[max(0, m.start() - 70) : m.start()]  # text right before the act name
        found = list(head.finditer(window))
        if found:
            best = found[-1].group(1)  # the match closest to the act mention
    return best


async def _resolve_provision(conn, tgt: Target, art: str | None):
    """Return the target provision id and confidence: the exact article or §, else the act's anchor."""
    if art:
        # Czech acts are labelled "§ N", except the constitutional acts, which use "Čl. N" (see
        # esbirka._default_label). We cannot know in advance which form the target uses, so try
        # both: an exact match first, then a label with the disambiguating suffix the walker may
        # have appended. EU acts always use "Article N".
        labels = [f"Article {art}"] if tgt.kind == "eu" else [f"§ {art}", f"Čl. {art}"]
        pid = await conn.fetchval(
            """SELECT p.id FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
               WHERE sd.external_id = $1
                 AND (p.label = ANY($2::text[])
                      OR p.label LIKE ANY(SELECT l || '(%' FROM unnest($2::text[]) AS l))
               ORDER BY (p.label = ANY($2::text[])) DESC, p.ordinal NULLS LAST LIMIT 1""",
            tgt.external_id,
            labels,
        )
        if pid:
            return pid, CONF_ARTICLE
    pid = await conn.fetchval(  # anchor fallback: first provision of the act
        """SELECT p.id FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
           WHERE sd.external_id = $1 ORDER BY p.ordinal NULLS LAST, p.label LIMIT 1""",
        tgt.external_id,
    )
    return (pid, CONF_ACT) if pid else (None, 0.0)


async def resolve_external_xrefs(*, dry_run: bool = False, rematerialize: bool = True) -> dict:
    """Link dangling external cross-references to provisions and return per-outcome counts.

    Resolution per dangling row has two tiers:

      1. Act: parse `to_external_ref` into a canonical document id.
           EU  "Directive 2014/65"            -> CELEX 32014L0065  (the four-digit token is the year)
               "Regulation (EU) No 600/2014"  -> CELEX 32014R0600
           CZ  "zákona č. 89/2012 Sb."        -> external_id 89/2012  (number/year in the Collection)
         If that document is not in the corpus the row stays dangling; we only link to acts we hold.
      2. Article: search the citing provision's text for "Article N of <that act>" (EU) or
         "§ N ... zákona č. <num> Sb." (CZ) and link to that exact provision. If no article is
         found, fall back to the target act's anchor provision (the first by ordinal).

    Only `to_provision`, `verified_by` and `confidence` are written, on resolved rows; then, if
    `rematerialize` and not `dry_run`, `materialize_layer_a()` turns them into `references` /
    `referenced_by` edges.
    """
    pool = await get_pool()
    stats = {
        "candidates": 0,
        "linked_article": 0,
        "linked_act": 0,
        "skipped_no_corpus_doc": 0,
        "skipped_unparseable": 0,
        "self_or_same_doc": 0,
    }
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT cr.id, cr.to_external_ref, cr.from_provision, cr.metadata,
                      p.text AS citing_text, p.source_document_id AS from_doc
               FROM cross_reference cr JOIN provision p ON p.id = cr.from_provision
               WHERE cr.ref_type = 'external' AND cr.to_provision IS NULL
                 AND cr.to_external_ref IS NOT NULL AND cr.from_provision IS NOT NULL"""
        )
        stats["candidates"] = len(rows)
        for r in rows:
            tgt = parse_target(r["to_external_ref"])
            if tgt is None:
                stats["skipped_unparseable"] += 1
                continue
            in_corpus = await conn.fetchval("SELECT id FROM source_document WHERE external_id = $1", tgt.external_id)
            if in_corpus is None:
                stats["skipped_no_corpus_doc"] += 1
                continue
            if in_corpus == r["from_doc"]:  # would be intra-act, not cross-act
                stats["self_or_same_doc"] += 1
                continue
            art = find_article(r["citing_text"], tgt)
            if art is None:
                # Rows cited by act name (detected_by='named-act', see esbirka._mine_xrefs) have no
                # act number in the text, so find_article finds nothing. The named-act miner stored
                # the nearest § or Čl. in metadata.named_art; use it so the citation keeps article
                # precision instead of falling back to the act anchor.
                meta = r["metadata"]
                if isinstance(meta, str):
                    import json as _json

                    try:
                        meta = _json.loads(meta)
                    except (ValueError, TypeError):
                        meta = {}
                if isinstance(meta, dict):
                    art = meta.get("named_art") or None
            pid, conf = await _resolve_provision(conn, tgt, art)
            if pid is None or pid == r["from_provision"]:
                stats["self_or_same_doc"] += 1
                continue
            stats["linked_article" if conf == CONF_ARTICLE else "linked_act"] += 1
            if not dry_run:
                await conn.execute(
                    """UPDATE cross_reference
                       SET to_provision = $1, confidence = $2, verified_by = $3
                       WHERE id = $4""",
                    pid,
                    conf,
                    _VERIFIER,
                    r["id"],
                )
    stats["linked_total"] = stats["linked_article"] + stats["linked_act"]
    if not dry_run and rematerialize and stats["linked_total"]:
        from pythia.graph.materialize import materialize_layer_a

        stats["graph_edges"] = (await materialize_layer_a())["total_edges"]
    return stats


async def unresolve() -> int:
    """Reverse every link this resolver made (sets to_provision back to NULL)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            """WITH u AS (UPDATE cross_reference SET to_provision = NULL, verified_by = NULL
                          WHERE verified_by = $1 RETURNING 1) SELECT count(*) FROM u""",
            _VERIFIER,
        )
    from pythia.graph.materialize import materialize_layer_a

    await materialize_layer_a()
    return n


def _main() -> None:
    ap = argparse.ArgumentParser(description="Resolve dangling cross-act citations to real provisions.")
    ap.add_argument("--dry-run", action="store_true", help="report what would link, write nothing")
    ap.add_argument("--unresolve", action="store_true", help="undo all links made by this resolver")
    a = ap.parse_args()

    async def run():
        if a.unresolve:
            print(f"unresolved {await unresolve()} rows; graph re-materialized")
        else:
            s = await resolve_external_xrefs(dry_run=a.dry_run)
            tag = "DRY-RUN (nothing written)" if a.dry_run else "APPLIED"
            print(f"=== cross-act citation linking — {tag} ===")
            print(f"  dangling external candidates : {s['candidates']}")
            print(f"  linked to exact article/§    : {s['linked_article']}")
            print(f"  linked to act anchor         : {s['linked_act']}")
            print(f"  linked TOTAL                 : {s['linked_total']}")
            print(f"  skipped (target not in corpus): {s['skipped_no_corpus_doc']}")
            print(f"  skipped (unparseable name)   : {s['skipped_unparseable']}")
            print(f"  skipped (self/same doc)      : {s['self_or_same_doc']}")
            if "graph_edges" in s:
                print(f"  graph re-materialized → {s['graph_edges']:,} edges")
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
