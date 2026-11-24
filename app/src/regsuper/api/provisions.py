"""Source viewer endpoint: one provision with its ancestors and cross-references.

The frontend's `/sources/[provision_id]` page renders the ancestors as a breadcrumb
("Directive 2011/61/EU > Chapter VI > Article 31 > Paragraph 1") above the provision text.
Provisions are read from the app database by default, or from the Pythia corpus database.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import text as sql_text

from regsuper.retrieval.expand import expand
from regsuper.storage.database import session_scope
from regsuper.storage.graph_db import get_graph_engine, graph_session_scope

router = APIRouter()

# Provision read from the app database: the default path, also used by the crosswalk fallback when
# a ?corpus=pythia id resolves only through provision_id_map.
_APP_ROW_SQL = """
    SELECT p.id, p.label, p.text, p.text_format,
           p.hierarchy_path::text, p.legal_hierarchy,
           p.jurisdiction, p.language,
           p.source_document_id, p.parent_id,
           sd.external_id, sd.title, sd.publisher,
           p.bindingness_tier, p.temporal_status
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE p.id = :pid
"""

# Crosswalk fallback. provision_id_map (migration 014_corpus_cutover) maps Pythia corpus ids to app
# provision ids. It is unique on the (nexus_id, nexus_corpus) pair, so the lookup must pin
# nexus_corpus='pythia'. The table lives in the app database (regsuper_id references
# provision.id), so it is queried on the app session, not the corpus engine.
_ID_MAP_SQL = """
    SELECT regsuper_id
      FROM provision_id_map
     WHERE nexus_id = :nid AND nexus_corpus = 'pythia'
"""

# Provision reads for the read-only Pythia corpus database. That database has no trust columns
# (bindingness_tier, temporal_status), so they are returned as NULL and the frontend shows no
# badge rather than a fabricated one. Ancestors are found by hierarchy_path LTREE prefix and
# ordered child first, matching expand(); the frontend reverses them for the breadcrumb.
_CORPUS_ROW_SQL = """
    SELECT p.id, p.label, p.text, p.text_format,
           p.hierarchy_path::text, p.legal_hierarchy,
           p.jurisdiction, p.language,
           p.source_document_id, p.parent_id,
           sd.external_id, sd.title, sd.publisher
      FROM provision p
      JOIN source_document sd ON sd.id = p.source_document_id
     WHERE p.id = :pid
"""
_CORPUS_ANCESTORS_SQL = """
    SELECT a.id, a.label, a.hierarchy_path::text
      FROM provision h
      JOIN provision a
        ON a.source_document_id = h.source_document_id
       AND a.hierarchy_path @> h.hierarchy_path
       AND a.id <> h.id
     WHERE h.id = :pid
     ORDER BY nlevel(a.hierarchy_path) DESC
"""
_CORPUS_XREFS_SQL = """
    SELECT t.id, t.label, t.hierarchy_path::text
      FROM cross_reference cr
      JOIN provision t ON t.id = cr.to_provision
     WHERE cr.from_provision = :pid AND cr.to_provision IS NOT NULL
     ORDER BY nlevel(t.hierarchy_path), t.ordinal
"""


class HierarchyNode(BaseModel):
    id: UUID
    label: str | None
    hierarchy_path: str


class ProvisionView(BaseModel):
    id: UUID
    label: str | None
    text: str
    text_format: str
    hierarchy_path: str
    legal_hierarchy: str
    jurisdiction: str
    language: str
    source_document_id: UUID
    source_external_id: str | None
    source_title: str
    source_publisher: str
    parent_id: UUID | None
    ancestors: list[HierarchyNode]
    cross_references: list[HierarchyNode]
    # Trust and temporal badges. NULL until the trust backfill has populated them; the frontend
    # then shows no badge rather than a fabricated one.
    bindingness_tier: str | None
    temporal_status: str | None


async def _serve_from_app_db(provision_id: UUID) -> ProvisionView | None:
    """Read ``provision_id`` from the app database as a ``ProvisionView``.

    Returns ``None`` when the id is absent, leaving the caller to choose between a 404 and a
    fallback. The crosswalk fallback reuses it, so a mapped id yields exactly the view a direct
    read would.
    """
    async with session_scope() as session:
        row = (await session.execute(sql_text(_APP_ROW_SQL), {"pid": provision_id})).first()
    if row is None:
        return None

    expanded = await expand(provision_id)
    ancestors = [
        HierarchyNode(id=a.id, label=a.label, hierarchy_path=a.hierarchy_path)
        for a in (expanded.ancestors if expanded else [])
    ]
    xrefs = [
        HierarchyNode(id=x.id, label=x.label, hierarchy_path=x.hierarchy_path)
        for x in (expanded.cross_references if expanded else [])
    ]
    return ProvisionView(
        id=row[0],
        label=row[1],
        text=row[2],
        text_format=row[3],
        hierarchy_path=row[4],
        legal_hierarchy=row[5],
        jurisdiction=row[6],
        language=row[7],
        source_document_id=row[8],
        parent_id=row[9],
        source_external_id=row[10],
        source_title=row[11] or "",
        source_publisher=row[12] or "",
        ancestors=ancestors,
        cross_references=xrefs,
        bindingness_tier=row[13],  # NULL until backfilled; the frontend then shows no badge
        temporal_status=row[14],
    )


async def resolve_via_map(provision_id: UUID) -> ProvisionView | None:
    """Resolve a Pythia corpus id to the matching app provision via ``provision_id_map``.

    Citation chips carry Pythia corpus ids. For legal domains not yet migrated to the Pythia
    corpus, the id has no corpus row but does have a crosswalk entry to the provision the app
    stores, so the chip still opens instead of dead-ending at a 404. Returns ``None`` (the caller
    answers 404) when there is no crosswalk row or the mapped provision no longer exists.
    """
    async with session_scope() as session:
        map_row = (await session.execute(sql_text(_ID_MAP_SQL), {"nid": provision_id})).first()
    if map_row is None:
        return None
    return await _serve_from_app_db(map_row[0])


@router.get("/api/provisions/{provision_id}", response_model=ProvisionView)
async def get_provision(
    provision_id: UUID,
    response: Response,
    corpus: str | None = Query(
        default=None,
        description=(
            "Which corpus to read. Default (None) reads the app DB. 'pythia' (alias "
            "'graph') reads the read-only Pythia corpus DB that backs the act-graph "
            "diagram, so the diagram's provision deep-links resolve for all 628 acts. "
            "When a 'pythia' id is absent in the corpus (an un-cutover domain), it is "
            "resolved across the seam via provision_id_map to the regsuper provision."
        ),
    ),
) -> ProvisionView:
    """Return one provision with its ancestor chain and cross-references.

    With `corpus=pythia` (or `graph`) the provision is read from the Pythia corpus database; if it
    is not there, it is resolved through the id crosswalk to an app provision. The
    `X-Provision-Corpus` response header (`pythia` or `regsuper`) then says which corpus served
    it. Returns 404 when the provision cannot be found.
    """
    # Corpus drill-down from the graph diagram: read the provision (same schema) from the
    # read-only Pythia corpus. Answers 404, not 500, when the corpus database is not configured.
    if corpus in ("pythia", "graph"):
        engine = get_graph_engine()
        if engine is not None:
            async with graph_session_scope() as gsession:
                row = (
                    await gsession.execute(sql_text(_CORPUS_ROW_SQL), {"pid": provision_id})
                ).first()
                if row is not None:
                    anc_rows = (
                        await gsession.execute(
                            sql_text(_CORPUS_ANCESTORS_SQL), {"pid": provision_id}
                        )
                    ).all()
                    xref_rows = (
                        await gsession.execute(sql_text(_CORPUS_XREFS_SQL), {"pid": provision_id})
                    ).all()
                    ancestors = [
                        HierarchyNode(id=a[0], label=a[1], hierarchy_path=a[2]) for a in anc_rows
                    ]
                    xrefs = [
                        HierarchyNode(id=x[0], label=x[1], hierarchy_path=x[2]) for x in xref_rows
                    ]
                    # Served directly from the Pythia corpus.
                    response.headers["X-Provision-Corpus"] = "pythia"
                    return ProvisionView(
                        id=row[0],
                        label=row[1],
                        text=row[2],
                        text_format=row[3],
                        hierarchy_path=row[4],
                        legal_hierarchy=row[5],
                        jurisdiction=row[6],
                        language=row[7],
                        source_document_id=row[8],
                        parent_id=row[9],
                        source_external_id=row[10],
                        source_title=row[11] or "",
                        source_publisher=row[12] or "",
                        ancestors=ancestors,
                        cross_references=xrefs,
                        bindingness_tier=None,
                        temporal_status=None,  # not in the corpus DB
                    )
        # The id is not in the Pythia corpus (or the corpus database is not configured): try
        # the crosswalk to an app provision. 404 with a reason only when neither resolves.
        fallback = await resolve_via_map(provision_id)
        if fallback is not None:
            response.headers["X-Provision-Corpus"] = "regsuper"
            return fallback
        raise HTTPException(
            status_code=404,
            detail=(
                f"provision {provision_id} not found in pythia corpus and has no "
                f"provision_id_map crosswalk to a regsuper provision"
            ),
        )

    # Default path: read from the app database. The provenance header is only set for
    # ?corpus requests.
    view = await _serve_from_app_db(provision_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"provision {provision_id} not found")
    return view


# Avoid an "unused import" if a future stub adds a typed response shape.
_ = Any
