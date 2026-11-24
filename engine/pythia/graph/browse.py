"""Faceted browse and navigation data for the regulation explorer.

A query-agnostic read layer: it projects the document taxonomy (seven axes) and the graph analytics
(centrality and Louvain communities) into one stable JSON document. No question is involved and no
new data is computed. The views are:

- facets: value counts per taxonomy axis (the browse sidebar: bindingness, type, domain, ...).
- regimes: the Louvain communities as navigable legal regimes, each with its most central member as
  anchor and the acts it spans. This is the "by area of law" entry point.
- spine: the most central provisions overall (the "these matter most" list).
- catalogue: every source document with its taxonomy and provision count.
- act_tree(external_id): one act's provision hierarchy, annotated with centrality.

Facets and catalogue cover all documents, so repealed law stays visible through the
`temporal_status` facet. Regimes and spine are projections of the current-law graph, because
centrality and communities are computed only on that graph.

Run:  .venv/bin/python -m pythia.graph.browse [act_external_id]   (no argument prints the index summary)
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date  # noqa: F401  used in annotations; get_type_hints needs it in scope

from pythia.db import close_pool, get_pool

# Scalar taxonomy axes on source_document (legal_domain is an array and is handled separately).
_SCALAR_AXES = (
    "jurisdiction",
    "document_type",
    "legal_hierarchy",
    "bindingness",
    "temporal_status",
    "issuing_authority",
    "territorial_scope",
)


def group_regime_docs(rows: list[dict], *, top: int = 3) -> dict[int, list[dict]]:
    """Group regime rows into ``{community: [{ext, provisions}, ...]}``, keeping the top `top` acts.

    Rows must arrive sorted by community, then by provision count descending.
    """
    out: dict[int, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["community"], [])
        if len(bucket) < top:
            bucket.append({"ext": r["ext"], "provisions": r["n"]})
    return out


def shape_facets(axis_rows: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drop NULL values per axis and sort by descending count, breaking ties by value.

    The deterministic order keeps the browse sidebar stable between requests.
    """
    out: dict[str, list[dict]] = {}
    for axis, rows in axis_rows.items():
        vals = [{"value": r["v"], "count": r["n"]} for r in rows if r["v"] is not None]
        vals.sort(key=lambda x: (-x["count"], str(x["value"])))
        out[axis] = vals
    return out


async def browse_index(*, top_spine: int = 20, top_regimes: int = 25) -> dict:
    """Build the browse index: facets, top regimes, the centrality spine and the full catalogue."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Facets: over all documents, so the temporal_status axis keeps repealed law visible.
        axis_rows: dict[str, list[dict]] = {}
        for axis in _SCALAR_AXES:
            axis_rows[axis] = [
                dict(r)
                for r in await conn.fetch(f"SELECT {axis} AS v, count(*) AS n FROM source_document GROUP BY {axis}")
            ]
        axis_rows["legal_domain"] = [
            dict(r)
            for r in await conn.fetch(
                "SELECT d AS v, count(*) AS n FROM source_document, unnest(legal_domain) d GROUP BY d"
            )
        ]
        facets = shape_facets(axis_rows)

        # Regimes (current-law communities): size and most central anchor. The size counts all
        # members, but the anchor pick excludes CASE_LAW so a court ruling can never be a regime's
        # "most important law" (hence the FILTER on array_agg rather than on the group).
        reg = await conn.fetch(
            """SELECT community, count(*) AS n,
                      (array_agg(id ORDER BY centrality DESC NULLS LAST)
                         FILTER (WHERE legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                                   AND is_anchor_only = false))[1] AS anchor_id
                 FROM provision WHERE community IS NOT NULL
                GROUP BY community ORDER BY n DESC LIMIT $1""",
            top_regimes,
        )
        anchor_ids = [r["anchor_id"] for r in reg]
        anchors = {
            str(x["id"]): dict(x)
            for x in await conn.fetch(
                """SELECT p.id::text id, p.label, sd.external_id ext, sd.title
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE p.id = ANY($1::uuid[])""",
                anchor_ids,
            )
        }
        span = group_regime_docs(
            [
                dict(r)
                for r in await conn.fetch(
                    """SELECT p.community, sd.external_id ext, count(*) AS n
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE p.community = ANY($1::int[])
                GROUP BY p.community, sd.external_id ORDER BY p.community, n DESC""",
                    [r["community"] for r in reg],
                )
            ]
        )
        regimes = []
        for r in reg:
            a = anchors.get(str(r["anchor_id"]), {})
            regimes.append(
                {
                    "community": r["community"],
                    "provisions": r["n"],
                    "anchor": {"ext": a.get("ext"), "label": a.get("label"), "title": (a.get("title") or "")[:70]},
                    "acts": span.get(r["community"], []),
                }
            )

        # Spine: the most central provisions overall. Court rulings and structural anchor rows are
        # excluded, so neither a ruling nor a bare structural node ('Část 10') lands on the
        # "these laws matter most" list.
        spine = [
            dict(r)
            for r in await conn.fetch(
                """SELECT p.label, p.community, round(p.centrality::numeric, 6) AS centrality,
                      sd.external_id ext, left(sd.title, 70) title
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE p.centrality IS NOT NULL
                  AND p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW'
                  AND p.is_anchor_only = false
                ORDER BY p.centrality DESC LIMIT $1""",
                top_spine,
            )
        ]

        # Catalogue: every document with its taxonomy and provision count.
        catalogue = [
            dict(r)
            for r in await conn.fetch(
                """SELECT sd.external_id ext, left(sd.title, 80) title, sd.jurisdiction,
                      sd.document_type, sd.legal_hierarchy, sd.bindingness, sd.temporal_status,
                      sd.is_authoritative, sd.legal_domain,
                      count(p.id) FILTER (WHERE p.is_anchor_only = false) AS provisions
                 FROM source_document sd LEFT JOIN provision p ON p.source_document_id = sd.id
                GROUP BY sd.id ORDER BY provisions DESC"""
            )
        ]

        # Totals, so the explorer can render "showing N of M" for the truncated regime and spine views.
        regimes_total = await conn.fetchval(
            "SELECT count(DISTINCT community) FROM provision WHERE community IS NOT NULL"
        )
        spine_total = await conn.fetchval(
            "SELECT count(*) FROM provision WHERE centrality IS NOT NULL "
            "AND legal_hierarchy IS DISTINCT FROM 'CASE_LAW' AND is_anchor_only = false"
        )
        # In-force provisions without centrality are isolated in the graph (no edges), so they appear
        # in no regime and not on the spine. They are still real, in-force law, reachable through the
        # catalogue and act_tree. Reporting the count lets the explorer say that regimes and spine are
        # not exhaustive instead of implying they cover all law.
        isolated_in_force = await conn.fetchval(
            "SELECT count(*) FROM provision WHERE centrality IS NULL "
            "AND (valid_to IS NULL OR valid_to > CURRENT_DATE) "
            "AND (valid_from IS NULL OR valid_from <= CURRENT_DATE)"
        )

    return {
        "facets": facets,
        "regimes": regimes,
        "spine": spine,
        "catalogue": catalogue,
        "counts": {
            "documents": len(catalogue),
            "regimes_total": regimes_total,
            "spine_total": spine_total,
            "spine_shown": len(spine),
            "regimes_shown": len(regimes),
            # Regimes and spine cover only graph-connected current law; isolated in-force
            # provisions appear only in the catalogue and act_tree.
            "isolated_in_force": isolated_in_force,
        },
    }


# Human labels for synthetic structural group nodes, keyed by ltree segment type (Czech statute
# structure). Unknown types fall back to the capitalized type token, so they still render sensibly.
_STRUCT_LABELS = {
    "kniha": "Kniha",
    "cast": "Část",
    "hlava": "Hlava",
    "dil": "Díl",
    "oddil": "Oddíl",
    "pododdil": "Pododdíl",
    "kapitola": "Kapitola",
    "par": "§",
    "clanek": "Článek",
    "cl": "Čl.",
    "art": "Article",
    "odst": "odst.",
    "pism": "písm.",
    "bod": "bod",
}


def _struct_label(segment: str) -> str:
    """Return the human label for a synthetic structural node from its ltree segment.

    Segments have the form ``type_number``: 'cast_10' becomes 'Část 10' and 'hlava_1' becomes
    'Hlava 1'. Unknown types use the capitalized type token.
    """
    typ, _, num = segment.partition("_")
    pretty = _STRUCT_LABELS.get(typ, typ.capitalize() or segment)
    return f"{pretty} {num}".strip() if num else pretty


def build_act_tree(rows: list[dict]) -> list[dict]:
    """Nest an act's provisions under the structural groups encoded in their hierarchy_path ltree.

    Czech acts carry no parent_id: the nesting into Část / Hlava / Díl / Oddíl lives only in the
    ltree label path (for example 'cast_10.hlava_1.dil_2.par_332'). The parent_id projection is
    therefore flat, with every § a sibling (700+ rows for a code). Here a node's parent is its longest
    proper ltree prefix. A prefix that is itself a real provision (an EU act whose Article rows exist)
    hosts its children directly; every other prefix becomes a synthetic structural group node.

    Rows must arrive in reading order (by ordinal). A group is created when its first descendant
    attaches, so real and synthetic siblings stay in document order.

    Returns:
        The root nodes, each with a ``children`` list.
    """
    nodes: dict[str, dict] = {}  # ltree path -> node
    roots: list[dict] = []

    # Pass 1: every real provision becomes a node keyed by its full ltree path, so a real ancestor
    # is never shadowed by a synthetic one.
    for r in rows:
        hp = r.get("hpath")
        node = {
            "kind": "provision",
            "synthetic": False,
            "id": r["id"],
            "label": r["label"],
            "ordinal": r["ordinal"],
            "centrality": r["centrality"],
            "is_definition": r["is_definition"],
            "path": hp,
            "children": [],
        }
        if hp:
            nodes[hp] = node
        else:  # defensive: a provision without an ltree path becomes a flat root
            roots.append(node)

    def _link(node: dict, path: str) -> None:
        if "." not in path:  # a single-segment path is a structural root
            roots.append(node)
        else:
            _ensure(path.rsplit(".", 1)[0])["children"].append(node)

    def _ensure(path: str) -> dict:
        """Return the node for an ltree `path`, creating a synthetic group if none exists.

        Missing ancestors are created recursively, and each new group is linked to its parent
        as soon as it is created.
        """
        hit = nodes.get(path)
        if hit is not None:
            return hit
        node = {
            "kind": "structure",
            "synthetic": True,
            "path": path,
            "label": _struct_label(path.rsplit(".", 1)[-1]),
            "children": [],
        }
        nodes[path] = node
        _link(node, path)
        return node

    # Pass 2 (reading order): link each real provision to its parent, creating ancestors lazily.
    for r in rows:
        hp = r.get("hpath")
        if hp:
            _link(nodes[hp], hp)
    return roots


async def act_tree(external_id: str, *, as_of: date | None = None) -> dict:
    """Return one act's provision hierarchy, ordered and annotated with centrality and child counts.

    `tree` is the nested structure the explorer renders: provisions grouped under the part, title,
    division and section groups encoded in their hierarchy_path ltree (without it, a Czech act, which
    has no parent_id, is a flat list of every §). `top_level` and `nodes` are the flat parent_id
    projection, kept for backward compatibility.

    `as_of` limits the result to provisions in force on that date. None returns every provision of
    the act, so repealed history stays visible.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        doc = await conn.fetchrow(
            "SELECT id, external_id, title, document_type, bindingness, temporal_status "
            "FROM source_document WHERE external_id = $1",
            external_id,
        )
        if not doc:
            return {"error": f"no document with external_id {external_id!r}"}
        rows = await conn.fetch(
            """SELECT p.id::text id, p.parent_id::text parent, p.label, p.ordinal,
                      p.hierarchy_path::text AS hpath,
                      round(p.centrality::numeric, 6) AS centrality, p.is_definition,
                      (SELECT count(*) FROM provision c WHERE c.parent_id = p.id) AS children
                 FROM provision p
                WHERE p.source_document_id = $1
                  AND p.is_anchor_only = false          -- B1a-03: build_act_tree still SYNTHESIZES the
                                                        -- structural groups from the ltree prefixes (kind=
                                                        -- 'structure'); the materialized anchor rows are the
                                                        -- persisted twin of those synthetic nodes and would
                                                        -- double them here, so the explorer view excludes them.
                  AND ($2::date IS NULL
                       OR ((p.valid_to   IS NULL OR p.valid_to   > $2::date)
                       AND (p.valid_from IS NULL OR p.valid_from <= $2::date)))
                ORDER BY p.ordinal NULLS LAST, p.hierarchy_path""",
            doc["id"],
            as_of,
        )
        raw = [dict(r) for r in rows]  # includes hpath, which the tree builder needs
        tree = build_act_tree(raw)
        # Flat projection for backward compatibility: drop the internal hpath so `nodes` and
        # `top_level` keep their established shape. The nesting lives in `tree`.
        all_nodes = [{k: v for k, v in n.items() if k != "hpath"} for n in raw]
        top = [n for n in all_nodes if n["parent"] is None]
    # `tree` is the nested ltree structure the UI renders. `nodes` and `top_level` are the flat
    # parent_id projection, kept for existing consumers and for EU acts that nest via parent_id.
    return {
        "document": {
            "ext": doc["external_id"],
            "title": doc["title"],
            "type": doc["document_type"],
            "bindingness": doc["bindingness"],
            "temporal_status": doc["temporal_status"],
        },
        "provisions_total": len(rows),
        "tree": tree,
        "top_level": top,
        "nodes": all_nodes,
    }


async def _main() -> None:
    if len(sys.argv) > 1:
        tree = await act_tree(sys.argv[1])
        print(json.dumps(tree, ensure_ascii=False, indent=2, default=str)[:2500])
        await close_pool()
        return
    ix = await browse_index()
    print(f"=== Nexus faceted-browse index ===  {ix['counts']['documents']} documents")
    print("\n-- facets (axis: top values) --")
    for axis, vals in ix["facets"].items():
        head = ", ".join(f"{v['value']}={v['count']}" for v in vals[:4])
        print(f"  {axis:18s} {head}{' …' if len(vals) > 4 else ''}")
    print("\n-- regimes (top, anchor = most-central member) --")
    for r in ix["regimes"][:8]:
        acts = " ".join(a["ext"] for a in r["acts"])
        print(
            f"  #{r['community']:<3d} {r['provisions']:4d} prov  {str(r['anchor']['ext']):11s} "
            f"{str(r['anchor']['label']):11s}  [{acts}]"
        )
    print("\n-- spine (most-central provisions) --")
    for s in ix["spine"][:8]:
        print(f"  {str(s['ext']):11s} {str(s['label']):11s} c={s['centrality']}  {s['title'][:42]}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
