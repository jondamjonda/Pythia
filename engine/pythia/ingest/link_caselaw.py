r"""Turn the statute citations stored with each court ruling into interprets / applies edges.

`justice_caselaw` stores each ruling's structured statute citations in its provision metadata. A
citation that resolves to an in-corpus provision becomes an `interprets` or `applies` edge (ruling to
statute §) plus its inverse; one that does not resolve becomes a typed dangling leaf. All edges go to
`pythia_edge` with source='case_law'. `resolve_xrefs` does the same for cross-act references between
statutes. Idempotent, and reversible with --down.

Run (from engine/):
  dry run:  .venv/bin/python -m pythia.ingest.link_caselaw --dry-run
  apply:    .venv/bin/python -m pythia.ingest.link_caselaw
  undo:     .venv/bin/python -m pythia.ingest.link_caselaw --down
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, field

from pythia.db import close_pool, get_pool
from pythia.graph.relations import BY_LABEL

_SOURCE = "case_law"
_VERIFIER = "pythia-caselaw-linker"

# Edge weight is the relation's base weight times confidence, as in materialize. Ruling citations are
# structured data, so confidence is 1.0.
_REL_INTERPRETS = BY_LABEL["interprets"]  # rel_id 21, w₀ 0.80
_REL_INTERPRETED_BY = BY_LABEL["interpreted_by"]  # rel_id 22, w₀ 0.70
_REL_APPLIES = BY_LABEL["applies"]  # rel_id 23, w₀ 0.55
_REL_APPLIED_BY = BY_LABEL["applied_by"]  # rel_id 24, w₀ 0.45
_CONF = 1.0  # deterministic JOIN on the court's own structured cite

# Extract the bare § number from a '§ N' label, allowing a letter suffix ('§ 115a') and odst./písm. tails.
_SECTION_LABEL = re.compile(r"^§\s*(\d+[a-z]?)", re.IGNORECASE)


_APEX_COURTS = ("Nejvyšší soud", "Nejvyšší správní soud", "Ústavní soud")


def _is_apex(court: str | None) -> bool:
    """Return True for the apex courts: Supreme Court, Supreme Administrative Court, Constitutional Court.

    Only they construe a statute authoritatively. A lower court (district, regional or municipal)
    applies it to the facts; Czech law has no stare decisis, so lower-court reasoning is not a
    binding construal. Rulings from the Ministry of Justice portal are lower-court decisions, so they
    always yield `applies`.
    """
    return any(a in (court or "") for a in _APEX_COURTS)


@dataclass
class RuleCite:
    """One ruling-to-statute citation, aggregated per (ruling, act, label) across the ruling's sections.

    The relation is chosen precision first (`is_construal`): `interprets` (weight 0.80) requires a
    citation in the court's own reasoning (odůvodnění, the statement of reasons, or právní věta, the
    headnote) by an apex court; anything else, including an unknown section, is the weaker `applies`
    (0.55), so a construal is never over-claimed. Sub-targets below the § (odst. and písm., subsection
    and letter) are kept in the edge metadata, since the corpus models provisions at the § level.
    """

    ruling_doc_id: str
    ruling_anchor_pid: str  # the ruling provision used as edge src (its first section)
    act_external_id: str
    label: str | None  # '§ N', or None for a whole-act citation
    is_reasoning: bool  # cited in any reasoning section (odůvodnění / právní věta)
    is_apex: bool  # deciding court is an apex court (has interpretive authority)
    raw_paragraph: str
    lex_type: str = ""
    sections: set[str] = field(default_factory=set)

    def is_construal(self) -> bool:
        return self.is_reasoning and self.is_apex

    def relation_label(self) -> str:
        return "interprets" if self.is_construal() else "applies"


def _cite_grain_key(act: str, label: str | None) -> str:
    return f"{act}|{label or '*'}"


async def _ruling_anchor(conn, doc_id: str):
    """Return the ruling provision used as edge src: its first section by ordinal.

    A ruling is one act of construal, so all its citations share this anchor, which keeps edges at
    the (ruling, target) grain.
    """
    return await conn.fetchval(
        "SELECT id FROM provision WHERE source_document_id=$1 ORDER BY ordinal NULLS LAST, label LIMIT 1", doc_id
    )


async def collect_cites(conn) -> list[RuleCite]:
    """Read every CASE_LAW ruling's stored citations and aggregate them per (ruling, act, label).

    The is_reasoning flag is OR-ed across the sections that carry the citation.
    """
    rows = await conn.fetch(
        """SELECT p.id AS pid, p.source_document_id AS doc_id, p.ordinal, p.metadata,
                  sd.issuing_authority AS court
             FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE sd.legal_hierarchy = 'CASE_LAW'
            ORDER BY p.source_document_id, p.ordinal NULLS LAST"""
    )
    anchors: dict[str, str] = {}
    agg: dict[str, RuleCite] = {}  # key: doc_id|act|label
    for r in rows:
        doc_id = str(r["doc_id"])
        anchors.setdefault(doc_id, str(r["pid"]))  # rows are ordered, so the first per doc is the anchor
        is_apex = _is_apex(r["court"])
        md = r["metadata"]
        md = json.loads(md) if isinstance(md, str) else (md or {})
        is_reasoning = bool(md.get("is_reasoning"))
        section = md.get("section") or ""
        for c in md.get("cites") or []:
            act = c.get("act")
            if not act:
                continue
            label = c.get("label")  # '§ N' or None
            k = f"{doc_id}|{_cite_grain_key(act, label)}"
            existing = agg.get(k)
            if existing is None:
                agg[k] = RuleCite(
                    ruling_doc_id=doc_id,
                    ruling_anchor_pid=anchors[doc_id],
                    act_external_id=act,
                    label=label,
                    is_reasoning=is_reasoning,
                    is_apex=is_apex,
                    raw_paragraph=str(c.get("raw_paragraph") or ""),
                    lex_type=str(c.get("lex_type") or ""),
                    sections={section},
                )
            else:
                existing.is_reasoning = existing.is_reasoning or is_reasoning
                existing.sections.add(section)
    # Re-point every citation at its document's anchor (setdefault above relied on row order).
    for c in agg.values():
        c.ruling_anchor_pid = anchors[c.ruling_doc_id]
    return list(agg.values())


async def resolve_target(conn, act_external_id: str, label: str | None):
    """Resolve a citation to an in-corpus provision id by joining on (act, '§ N').

    This is a deterministic join, not NLP: the connector has already turned {lexNumber}/{lexYear}
    into the act external_id 'NUM/YEAR' and paragraphNumber into the label '§ N'. Returns
    (provision_id, resolved_act_external_id) or (None, None). A whole-act citation (label None) names
    no specific § and stays dangling; it is deliberately not snapped to § 1, which would assert a
    construal the ruling never made. The join is keyed on the citation's own act, so a § that exists
    only in a different act can never match (act scoping).
    """
    if label is None:
        return None, None
    m = _SECTION_LABEL.match(label)
    bare = f"§ {m.group(1)}" if m else label
    row = await conn.fetchrow(
        """SELECT p.id, sd.external_id
             FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
            WHERE sd.external_id = $1
              AND (p.label = $2 OR p.label = $3 OR p.label LIKE $3 || ' %' OR p.label LIKE $3 || '(%')
            ORDER BY (p.label = $3) DESC, (p.label = $2) DESC, p.ordinal NULLS LAST
            LIMIT 1""",
        act_external_id,
        label,
        bare,
    )
    if row is None:
        return None, None
    return str(row["id"]), row["external_id"]


@dataclass
class Edge:
    src: str
    dst: str | None
    dst_external: str | None
    rel_label: str
    rel_id: int
    weight: float
    metadata: dict


def _build_edges(cite: RuleCite, target_pid: str | None, resolved_act: str | None) -> list[Edge]:
    """Build the forward and inverse edges for a resolved citation, or one dangling-leaf forward edge.

    A resolved citation yields `interprets` / `applies` plus its inverse `interpreted_by` /
    `applied_by`; they carry source='case_law' so the materialize step's scoped delete preserves
    them. A citation that does not resolve (act outside the corpus, or a whole act with no §) yields
    a forward edge with dst NULL and dst_external set; a destination is never invented. As a
    defensive act-scoping check, a resolved target whose act differs from the citation's act is
    treated as dangling. The join already keys on the citation's act, so this only fires on a
    corrupt row.
    """
    construal = cite.is_construal()
    fwd = _REL_INTERPRETS if construal else _REL_APPLIES
    inv = _REL_INTERPRETED_BY if construal else _REL_APPLIED_BY
    meta = {
        "ruling_doc": cite.ruling_doc_id,
        "act": cite.act_external_id,
        "cited_label": cite.label,
        "raw_paragraph": cite.raw_paragraph,
        "lex_type": cite.lex_type,
        "sections": sorted(cite.sections),
        "interpretation_basis": "apex_reasoning" if construal else "application",
        "verified_by": _VERIFIER,
    }
    if target_pid is not None and resolved_act == cite.act_external_id and target_pid != cite.ruling_anchor_pid:
        return [
            Edge(
                cite.ruling_anchor_pid,
                target_pid,
                None,
                fwd.label,
                fwd.rel_id,
                round(fwd.default_weight * _CONF, 6),
                meta,
            ),
            Edge(
                target_pid,
                cite.ruling_anchor_pid,
                None,
                inv.label,
                inv.rel_id,
                round(inv.default_weight * _CONF, 6),
                meta,
            ),
        ]
    # Dangling leaf (act outside the corpus, or a whole-act citation): dst NULL, and dst_external keeps
    # the citation as written ('NUM/YEAR' plus the § if any) so the leaf stays typed rather than lost.
    dst_external = cite.act_external_id + (f" {cite.label}" if cite.label else "")
    return [
        Edge(
            cite.ruling_anchor_pid,
            None,
            dst_external,
            fwd.label,
            fwd.rel_id,
            round(fwd.default_weight * _CONF, 6),
            {**meta, "dangling_reason": "out_of_corpus_or_whole_act"},
        )
    ]


async def link_caselaw(*, dry_run: bool = False) -> dict:
    """Resolve every stored ruling citation and, unless `dry_run`, rewrite the case-law edges.

    Idempotent: existing source='case_law' edges are deleted first, then edges are inserted with ON
    CONFLICT DO NOTHING. No double edges: a ruling citation lives only in ruling-provision metadata,
    never as a cross_reference row, so materialize's `references` inserts can never emit a generic
    edge for it. This linker is the only writer of a ruling citation's edge and writes a single
    relation per (ruling, target). Returns counts and a few samples.
    """
    pool = await get_pool()
    stats = {
        "ruling_cites": 0,
        "resolved_interprets": 0,
        "resolved_applies": 0,
        "dangling_out_of_corpus": 0,
        "dangling_whole_act": 0,
        "dangling_section_not_held": 0,
        "edges_written": 0,
        "rulings": 0,
        "samples": [],
    }
    async with pool.acquire() as conn:
        corpus_acts = {r["external_id"] for r in await conn.fetch("SELECT external_id FROM source_document")}
        cites = await collect_cites(conn)
        stats["ruling_cites"] = len(cites)
        stats["rulings"] = len({c.ruling_doc_id for c in cites})
        edges: list[Edge] = []
        for c in cites:
            pid, racts = await resolve_target(conn, c.act_external_id, c.label)
            built = _build_edges(c, pid, racts)
            if pid is not None and built[0].dst is not None:
                if c.is_reasoning:
                    stats["resolved_interprets"] += 1
                else:
                    stats["resolved_applies"] += 1
            elif c.label is None:
                stats["dangling_whole_act"] += 1  # whole-act citation (no §), never snapped to a §
            elif c.act_external_id in corpus_acts:
                stats["dangling_section_not_held"] += 1  # act is in the corpus, this § is not
            else:
                stats["dangling_out_of_corpus"] += 1  # the act itself is not in the corpus
            edges.extend(built)
            if len(stats["samples"]) < 12:
                stats["samples"].append(
                    {
                        "act": c.act_external_id,
                        "label": c.label,
                        "relation": c.relation_label(),
                        "resolved": pid is not None and built[0].dst is not None,
                        "dst_external": built[0].dst_external,
                    }
                )
        if not dry_run:
            async with conn.transaction():
                await conn.execute("DELETE FROM pythia_edge WHERE source = $1", _SOURCE)
                for e in edges:
                    await conn.execute(
                        """INSERT INTO pythia_edge
                               (src, dst, dst_external, rel_id, edge_type, weight, confidence, source, metadata)
                           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                           ON CONFLICT DO NOTHING""",
                        e.src,
                        e.dst,
                        e.dst_external,
                        e.rel_id,
                        e.rel_label,
                        e.weight,
                        _CONF,
                        _SOURCE,
                        json.dumps(e.metadata, ensure_ascii=False),
                    )
                stats["edges_written"] = await conn.fetchval(
                    "SELECT count(*) FROM pythia_edge WHERE source = $1", _SOURCE
                )
        else:
            stats["edges_written"] = len(edges)
    stats["resolved_total"] = stats["resolved_interprets"] + stats["resolved_applies"]
    stats["dangling_total"] = (
        stats["dangling_out_of_corpus"] + stats["dangling_whole_act"] + stats["dangling_section_not_held"]
    )
    return stats


async def down() -> int:
    """Drop every case-law edge this linker wrote and return how many were removed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "WITH d AS (DELETE FROM pythia_edge WHERE source=$1 RETURNING 1) SELECT count(*) FROM d", _SOURCE
        )
    return n


def _print(s: dict, *, dry_run: bool) -> None:
    tag = "DRY-RUN (nothing written)" if dry_run else "APPLIED"
    print(f"=== case-law linking — {tag} ===")
    print(f"  rulings                       : {s['rulings']}")
    print(f"  ruling cites (doc,act,§ grain): {s['ruling_cites']}")
    print(f"  resolved → interprets         : {s['resolved_interprets']}")
    print(f"  resolved → applies            : {s['resolved_applies']}")
    print(f"  resolved TOTAL                : {s['resolved_total']}")
    print(f"  dangling (whole-act / no §)   : {s['dangling_whole_act']}")
    print(f"  dangling (§ not held in act)  : {s['dangling_section_not_held']}")
    print(f"  dangling (out-of-corpus act)  : {s['dangling_out_of_corpus']}")
    print(f"  dangling TOTAL                : {s['dangling_total']}")
    print(f"  edges {'to write' if dry_run else 'written'} (fwd+inv+leaf) : {s['edges_written']}")
    for smp in s["samples"]:
        kind = "RESOLVED" if smp["resolved"] else "dangling"
        tgt = "" if smp["resolved"] else f" → leaf[{smp['dst_external']}]"
        print(f"    · {smp['act']} {smp['label'] or '(whole act)'}  {smp['relation']:10s} {kind}{tgt}")


def _main() -> None:
    ap = argparse.ArgumentParser(description="Resolve ruling cites to interprets/applies edges.")
    ap.add_argument("--dry-run", action="store_true", help="report what would link, write nothing")
    ap.add_argument("--down", action="store_true", help="drop every case-law edge this linker wrote")
    args = ap.parse_args()

    async def run():
        if args.down:
            print(f"⏪ removed {await down()} case-law edges")
        else:
            s = await link_caselaw(dry_run=args.dry_run)
            _print(s, dry_run=args.dry_run)
        await close_pool()

    asyncio.run(run())


if __name__ == "__main__":
    _main()
