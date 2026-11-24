"""Read-only, control-token-gated endpoints behind the interactive act-graph diagram.

They query the Pythia corpus database directly through the read-only engine in
``regsuper.storage.graph_db``, so they work without the Pythia engine service. When
``pythia_database_url`` is unset they answer 200 with ``{"enabled": false, "reason": ...}`` so
the UI can hide the panel. Act-level edge sets are small (about 1k pairs), so no pagination.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from regsuper.api.admin import require_control_token
from regsuper.config import get_settings
from regsuper.storage.graph_db import get_graph_engine, graph_session_scope

router = APIRouter(prefix="/admin/api/graph", tags=["admin", "graph"])

# Corpus facts verified against the Pythia database (June 2026): the largest act holds 3,409
# provisions and the hierarchy_path LTREE reaches depth 6. Reported in the /acts stats so the UI
# can size its lazy-expand controls without a probe query.
_MAX_PROVISIONS_PER_ACT = 3409
_MAX_DEPTH = 6

_DISABLED = {"enabled": False, "reason": "PYTHIA_DATABASE_URL not configured"}


def _enabled() -> bool:
    return bool(get_settings().pythia_database_url) and get_graph_engine() is not None


# ---------------------------------------------------------------------------
# GET /acts: the act-level graph of nodes and edges.
# ---------------------------------------------------------------------------
_NODES_SQL = text(
    """
    SELECT
        sd.id::text                          AS id,
        sd.external_id                       AS ext,
        sd.title                             AS title,
        sd.jurisdiction                      AS jurisdiction,
        sd.legal_hierarchy                   AS legal_hierarchy,
        sd.document_type                     AS document_type,
        COALESCE(pc.provision_count, 0)      AS provision_count,
        sd.valid_from                        AS valid_from,
        sd.valid_to                          AS valid_to,
        sd.superseded_by::text               AS superseded_by
    FROM source_document sd
    LEFT JOIN (
        SELECT source_document_id, count(*) AS provision_count
        FROM provision
        GROUP BY source_document_id
    ) pc ON pc.source_document_id = sd.id
    ORDER BY sd.title
    """
)

_EDGES_XREF_SQL = text(
    """
    SELECT
        pf.source_document_id::text          AS "from",
        pt.source_document_id::text          AS "to",
        count(*)                             AS weight,
        array_agg(DISTINCT cr.ref_type)      AS types,
        avg(cr.confidence)                   AS avg_confidence
    FROM cross_reference cr
    JOIN provision pf ON pf.id = cr.from_provision
    JOIN provision pt ON pt.id = cr.to_provision
    WHERE cr.to_provision IS NOT NULL
      AND pf.source_document_id <> pt.source_document_id
    GROUP BY pf.source_document_id, pt.source_document_id
    HAVING count(*) >= :min_weight
    """
)

_EDGES_PYTHIA_SQL = text(
    r"""
    SELECT
        ps.source_document_id::text          AS "from",
        pd.source_document_id::text          AS "to",
        count(*)                             AS weight,
        array_agg(DISTINCT e.edge_type)      AS types,
        avg(e.confidence)                    AS avg_confidence
    FROM pythia_edge e
    JOIN provision ps ON ps.id = e.src
    JOIN provision pd ON pd.id = e.dst
    WHERE e.dst IS NOT NULL
      AND ps.source_document_id <> pd.source_document_id
      AND e.edge_type NOT LIKE '%\_by'
      AND (:rel_filter = FALSE OR e.edge_type = ANY(:rel))
    GROUP BY ps.source_document_id, pd.source_document_id
    HAVING count(*) >= :min_weight
    """
)

# Distinct forward edge types between acts in pythia_edge; they drive the UI filter chips.
# A cheap DISTINCT, computed once per /acts call.
_REL_TYPES_SQL = text(
    r"""
    SELECT DISTINCT e.edge_type AS edge_type
    FROM pythia_edge e
    JOIN provision ps ON ps.id = e.src
    JOIN provision pd ON pd.id = e.dst
    WHERE e.dst IS NOT NULL
      AND ps.source_document_id <> pd.source_document_id
      AND e.edge_type NOT LIKE '%\_by'
    ORDER BY e.edge_type
    """
)


@router.get("/acts")
async def graph_acts(
    edge_source: str = Query("cross_reference"),
    min_weight: int = Query(1),
    rel: list[str] | None = Query(None),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Return the full act-level graph: one node per act and aggregated edges between acts.

    Edges come from resolved citations (``edge_source=cross_reference``, the default) or from the
    typed semantic graph (``edge_source=pythia_edge``). ``rel`` filters edge types and applies only
    to ``pythia_edge``.
    """
    if not _enabled():
        return dict(_DISABLED)

    use_pythia = edge_source == "pythia_edge"
    async with graph_session_scope() as session:
        node_rows = (await session.execute(_NODES_SQL)).mappings().all()
        if use_pythia:
            rel_filter = bool(rel)
            edge_rows = (
                (
                    await session.execute(
                        _EDGES_PYTHIA_SQL,
                        {
                            "min_weight": min_weight,
                            "rel_filter": rel_filter,
                            "rel": rel or [],
                        },
                    )
                )
                .mappings()
                .all()
            )
        else:
            edge_rows = (
                (await session.execute(_EDGES_XREF_SQL, {"min_weight": min_weight}))
                .mappings()
                .all()
            )
        rel_type_rows = (await session.execute(_REL_TYPES_SQL)).mappings().all()

    nodes = [
        {
            "id": r["id"],
            "ext": r["ext"],
            "title": r["title"],
            "jurisdiction": r["jurisdiction"],
            "legal_hierarchy": r["legal_hierarchy"],
            "document_type": r["document_type"],
            "provision_count": int(r["provision_count"]),
            "valid_from": r["valid_from"].isoformat() if r["valid_from"] else None,
            "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None,
            "superseded_by": r["superseded_by"],
        }
        for r in node_rows
    ]
    edges = [
        {
            "from": r["from"],
            "to": r["to"],
            "weight": int(r["weight"]),
            "types": list(r["types"]) if r["types"] else [],
            "avg_confidence": float(r["avg_confidence"])
            if r["avg_confidence"] is not None
            else None,
        }
        for r in edge_rows
    ]
    return {
        "enabled": True,
        "corpus": "pythia",
        "edge_source": "pythia_edge" if use_pythia else "cross_reference",
        "nodes": nodes,
        "edges": edges,
        "available_rel_types": [r["edge_type"] for r in rel_type_rows],
        "stats": {
            "n_acts": len(nodes),
            "n_edges": len(edges),
            "max_provisions_per_act": _MAX_PROVISIONS_PER_ACT,
            "max_depth": _MAX_DEPTH,
        },
    }


# ---------------------------------------------------------------------------
# GET /act/{act_id}: the nested provision tree of one act.
# ---------------------------------------------------------------------------
_DOC_SQL = text(
    """
    SELECT
        sd.id::text                                  AS id,
        sd.external_id                               AS ext,
        sd.title                                     AS title,
        sd.jurisdiction                              AS jurisdiction,
        sd.legal_hierarchy                           AS legal_hierarchy,
        sd.document_type                             AS document_type,
        (SELECT count(*) FROM provision p
           WHERE p.source_document_id = sd.id)       AS provisions_total
    FROM source_document sd
    WHERE sd.id = :id
    """
)

_TREE_SQL = text(
    """
    SELECT
        id::text                AS id,
        label                   AS label,
        eid                     AS eid,
        hierarchy_path::text    AS path,
        nlevel(hierarchy_path)  AS depth,
        ordinal                 AS ordinal,
        left(text, 140)         AS preview
    FROM provision
    WHERE source_document_id = :id
    ORDER BY hierarchy_path, ordinal
    """
)


def _build_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest provisions by LTREE path prefix, because ``parent_id`` is unreliable.

    A node's parent is the row whose ``path`` is the longest strict ancestor prefix of the node's
    path (compared on the dotted LTREE labels); roots have no such ancestor. ``kind`` is
    ``"structure"`` for nodes that have children, otherwise ``"provision"``.
    """
    nodes: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for r in rows:
        node = {
            "id": r["id"],
            "label": r["label"],
            "eid": r["eid"],
            "path": r["path"],
            "depth": int(r["depth"]),
            "ordinal": int(r["ordinal"]),
            "preview": r["preview"],
            "kind": "provision",
            "children": [],
        }
        nodes[r["path"]] = node
        ordered.append(node)

    roots: list[dict[str, Any]] = []
    for node in ordered:
        path = node["path"]
        parts = path.split(".")
        parent = None
        # Walk from the longest possible ancestor prefix down to the shortest;
        # the first existing one is this node's parent.
        for cut in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in nodes and candidate != path:
                parent = nodes[candidate]
                break
        if parent is not None:
            parent["children"].append(node)
            parent["kind"] = "structure"
        else:
            roots.append(node)
    return roots


@router.get("/act/{act_id}")
async def graph_act(
    act_id: str,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Return the full nested provision tree of one act, identified by its source document UUID.

    The tree is built from ``hierarchy_path``. Even the largest act (3,409 provisions over 6
    levels) is returned in one response; the UI expands nodes lazily.
    """
    if not _enabled():
        return dict(_DISABLED)

    async with graph_session_scope() as session:
        doc_row = (await session.execute(_DOC_SQL, {"id": act_id})).mappings().first()
        if doc_row is None:
            return {"enabled": True, "found": False, "act_id": act_id}
        tree_rows = (await session.execute(_TREE_SQL, {"id": act_id})).mappings().all()

    return {
        "enabled": True,
        "found": True,
        "document": {
            "id": doc_row["id"],
            "ext": doc_row["ext"],
            "title": doc_row["title"],
            "jurisdiction": doc_row["jurisdiction"],
            "legal_hierarchy": doc_row["legal_hierarchy"],
            "document_type": doc_row["document_type"],
            "provisions_total": int(doc_row["provisions_total"]),
        },
        "tree": _build_tree([dict(r) for r in tree_rows]),
    }


# ---------------------------------------------------------------------------
# GET /act/{act_id}/links: inbound and outbound act-level links with examples.
# ---------------------------------------------------------------------------
_ACT_HEAD_SQL = text(
    """
    SELECT id::text AS id, external_id AS ext, title AS title
    FROM source_document WHERE id = :id
    """
)

# Every inter-act provision pair that touches the act under inspection (``:self``) on either side.
# _aggregate_links splits the rows by direction and keeps up to 3 examples per linked act.
_LINKS_XREF_SQL = text(
    """
    WITH e AS (
        SELECT
            pf.source_document_id AS from_doc,
            pt.source_document_id AS to_doc,
            cr.from_provision::text AS from_provision,
            pf.label AS from_label,
            cr.to_provision::text AS to_provision,
            pt.label AS to_label,
            cr.ref_type AS rel
        FROM cross_reference cr
        JOIN provision pf ON pf.id = cr.from_provision
        JOIN provision pt ON pt.id = cr.to_provision
        WHERE cr.to_provision IS NOT NULL
          AND pf.source_document_id <> pt.source_document_id
          AND (pf.source_document_id = :self OR pt.source_document_id = :self)
    )
    SELECT * FROM e
    """
)

_LINKS_PYTHIA_SQL = text(
    r"""
    WITH e AS (
        SELECT
            ps.source_document_id AS from_doc,
            pd.source_document_id AS to_doc,
            e.src::text AS from_provision,
            ps.label AS from_label,
            e.dst::text AS to_provision,
            pd.label AS to_label,
            e.edge_type AS rel
        FROM pythia_edge e
        JOIN provision ps ON ps.id = e.src
        JOIN provision pd ON pd.id = e.dst
        WHERE e.dst IS NOT NULL
          AND ps.source_document_id <> pd.source_document_id
          AND e.edge_type NOT LIKE '%\_by'
          AND (ps.source_document_id = :self OR pd.source_document_id = :self)
    )
    SELECT * FROM e
    """
)

_DANGLING_SQL = text(
    """
    SELECT cr.to_external_ref AS to_external_ref, count(*) AS count
    FROM cross_reference cr
    JOIN provision pf ON pf.id = cr.from_provision
    WHERE cr.to_provision IS NULL
      AND cr.to_external_ref IS NOT NULL
      AND pf.source_document_id = :self
    GROUP BY cr.to_external_ref
    ORDER BY count DESC
    """
)

# Titles and external ids of the linked acts referenced by the aggregation.
_DOC_LOOKUP_SQL = text(
    """
    SELECT id::text AS id, external_id AS ext, title AS title
    FROM source_document WHERE id = ANY(:ids)
    """
)


def _aggregate_links(
    rows: list[dict[str, Any]], self_id: str, *, outbound: bool
) -> dict[str, dict[str, Any]]:
    """Group example rows into one bucket per linked act, for one direction.

    When ``outbound`` is true this act is the source (``from_doc == self``) and rows are bucketed
    by ``to_doc``; otherwise this act is the target and rows are bucketed by ``from_doc``. Each
    bucket keeps up to 3 example provision pairs.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        if outbound:
            if str(r["from_doc"]) != self_id:
                continue
            other = str(r["to_doc"])
        else:
            if str(r["to_doc"]) != self_id:
                continue
            other = str(r["from_doc"])
        b = buckets.get(other)
        if b is None:
            b = {"_other": other, "weight": 0, "_types": set(), "examples": []}
            buckets[other] = b
        b["weight"] += 1
        b["_types"].add(r["rel"])
        if len(b["examples"]) < 3:
            b["examples"].append(
                {
                    "from_provision": r["from_provision"],
                    "from_label": r["from_label"],
                    "to_provision": r["to_provision"],
                    "to_label": r["to_label"],
                    "rel": r["rel"],
                }
            )
    return buckets


@router.get("/act/{act_id}/links")
async def graph_act_links(
    act_id: str,
    edge_source: str = Query("cross_reference"),
    direction: str = Query("both"),
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Return the inbound and outbound links of one act, each with up to 3 example provision pairs.

    ``dangling_out`` (cross_reference edges only) reports references from this act that could not
    be resolved to a provision in the corpus.
    """
    if not _enabled():
        return dict(_DISABLED)

    use_pythia = edge_source == "pythia_edge"
    want_out = direction in ("out", "both")
    want_in = direction in ("in", "both")

    async with graph_session_scope() as session:
        head = (await session.execute(_ACT_HEAD_SQL, {"id": act_id})).mappings().first()
        if head is None:
            return {"enabled": True, "found": False, "act_id": act_id}

        link_sql = _LINKS_PYTHIA_SQL if use_pythia else _LINKS_XREF_SQL
        link_rows = [
            dict(r) for r in (await session.execute(link_sql, {"self": act_id})).mappings().all()
        ]

        out_buckets = _aggregate_links(link_rows, act_id, outbound=True) if want_out else {}
        in_buckets = _aggregate_links(link_rows, act_id, outbound=False) if want_in else {}

        # Resolve linked-act titles in one round-trip.
        other_ids = list({*out_buckets.keys(), *in_buckets.keys()})
        doc_lookup: dict[str, dict[str, Any]] = {}
        if other_ids:
            for r in (await session.execute(_DOC_LOOKUP_SQL, {"ids": other_ids})).mappings().all():
                doc_lookup[r["id"]] = {"ext": r["ext"], "title": r["title"]}

        dangling: list[dict[str, Any]] = []
        if want_out and not use_pythia:
            dangling = [
                {"to_external_ref": r["to_external_ref"], "count": int(r["count"])}
                for r in (await session.execute(_DANGLING_SQL, {"self": act_id})).mappings().all()
            ]

    def _render(buckets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for other, b in buckets.items():
            meta = doc_lookup.get(other, {})
            out.append(
                {
                    "to": other,
                    "to_ext": meta.get("ext"),
                    "to_title": meta.get("title"),
                    "weight": b["weight"],
                    "types": sorted(t for t in b["_types"] if t is not None),
                    "examples": b["examples"],
                }
            )
        out.sort(key=lambda x: x["weight"], reverse=True)
        return out

    return {
        "enabled": True,
        "found": True,
        "edge_source": "pythia_edge" if use_pythia else "cross_reference",
        "act": {"id": head["id"], "ext": head["ext"], "title": head["title"]},
        "outbound": _render(out_buckets),
        "inbound": _render(in_buckets),
        "dangling_out": dangling,
    }
