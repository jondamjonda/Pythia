"""Debug retrieval endpoint (`POST /internal/retrieve`) and the public corpus overview.

`/internal/retrieve` drives the `retrieve()` orchestrator (query rewrite, HyDE, RRF fusion, Cohere
rerank, calibrated refusal). It is a debug surface, not part of the end-user analysis cascade,
whose candidate step (`analysis/l1_candidates.py`) is a plain metadata SQL query; the `refused`,
`flagged` and threshold fields therefore describe this path only. Keep the response shape stable:
the acceptance SQL, the calibration materialiser and the smoke harness read `results[*].label`.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text

from regsuper.api.admin import require_control_token
from regsuper.retrieval import RetrievalFilters, retrieve
from regsuper.storage.database import session_scope

router = APIRouter()


@router.get("/internal/corpus-overview")
async def corpus_overview() -> dict[str, Any]:
    """List the acts in the searchable corpus with their provision counts.

    Public and read-only, so the product page can tell users plainly what they can ask about.
    """
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT sd.external_id AS ext, sd.title AS title, sd.jurisdiction AS jurisdiction, "
                        "       sd.legal_hierarchy AS hierarchy, count(p.id) AS provisions "
                        "  FROM source_document sd LEFT JOIN provision p ON p.source_document_id = sd.id "
                        " GROUP BY sd.id ORDER BY count(p.id) DESC"
                    )
                )
            )
            .mappings()
            .all()
        )
        total_p = (await session.execute(text("SELECT count(*) FROM provision"))).scalar()
    acts = [
        {
            "ext": r["ext"],
            "title": r["title"],
            "jurisdiction": r["jurisdiction"],
            "hierarchy": r["hierarchy"],
            "provisions": int(r["provisions"]),
        }
        for r in rows
    ]
    return {"documents": len(acts), "provisions": int(total_p or 0), "acts": acts}


class RetrieveRequest(BaseModel):
    query: str = Field(..., min_length=1)
    jurisdiction: list[str] | str | None = None
    sector: str | None = None
    sector_tags: list[str] | None = None
    legal_hierarchy: list[str] | None = None
    active_only: bool = True
    # Point in time: retrieve the law as it stood on this date. When set, it defines the temporal
    # window and overrides `active_only` (see hybrid.metadata_filter_sql). None means current law.
    as_of: date | None = None
    project_id: UUID | None = None
    # Matches the orchestrator's default of 30 so this endpoint reflects what the pipeline
    # actually runs. Callers may request up to 200.
    top_n_rrf: int = Field(default=30, ge=1, le=200)
    top_n_rerank: int = Field(default=10, ge=1, le=50)
    expand_top_n: int = Field(default=10, ge=0, le=50)
    use_cache: bool = True


def _filters_from_request(req: RetrieveRequest) -> RetrievalFilters:
    juris: list[str] = []
    if isinstance(req.jurisdiction, str) and req.jurisdiction:
        juris = [j.strip() for j in req.jurisdiction.replace("+", ",").split(",") if j.strip()]
    elif isinstance(req.jurisdiction, list):
        juris = [j for j in req.jurisdiction if j]
    # `sector` is a project-level hint that the LLM legs (HyDE, rewrite) use as prompt context;
    # it is not a provision filter. Promoting it into sector_tags would return nothing for
    # every provision that has no sector tags yet, so only `sector_tags` given explicitly
    # constrain the search.
    return RetrievalFilters(
        jurisdictions=juris,
        sector_tags=list(req.sector_tags or []),
        legal_hierarchy=list(req.legal_hierarchy or []),
        active_only=req.active_only,
        as_of=req.as_of,
    )


@router.post("/internal/retrieve")
async def post_retrieve(
    req: RetrieveRequest,
    _: None = Depends(require_control_token),
) -> dict[str, Any]:
    """Run the full retrieval pipeline and return the reranked and expanded candidates.

    A debug endpoint: no token is needed in local dev, and everywhere else the request must carry
    a matching `X-Control-Token` header. When `refused` is true the candidates are still returned
    so an operator can inspect what the system would have answered with.
    """
    result = await retrieve(
        req.query,
        jurisdiction=", ".join(req.jurisdiction)
        if isinstance(req.jurisdiction, list)
        else (req.jurisdiction or ""),
        sector=req.sector or "",
        filters=_filters_from_request(req),
        project_id=req.project_id,
        top_n_rrf=req.top_n_rrf,
        top_n_rerank=req.top_n_rerank,
        expand_top_n=req.expand_top_n,
        use_cache=req.use_cache,
    )

    payloads = result.payloads
    results_out: list[dict[str, Any]] = []
    for rank, hit in enumerate(result.candidates, start=1):
        meta = payloads.get(hit.provision_id) or {}
        results_out.append(
            {
                "rank": rank,
                "provision_id": str(hit.provision_id),
                "label": meta.get("label"),
                "external_id": meta.get("external_id"),
                "title": meta.get("title"),
                "legal_hierarchy": meta.get("legal_hierarchy"),
                "jurisdiction": meta.get("jurisdiction"),
                "rrf_score": hit.rrf_score,
                "rerank_score": hit.rerank_score,
            }
        )

    expanded_out: list[dict[str, Any]] = []
    for exp in result.expanded:
        expanded_out.append(
            {
                "hit": {
                    "id": str(exp.hit.id),
                    "label": exp.hit.label,
                    "hierarchy_path": exp.hit.hierarchy_path,
                    "text": exp.hit.text,
                },
                "ancestors": [
                    {
                        "id": str(a.id),
                        "label": a.label,
                        "hierarchy_path": a.hierarchy_path,
                    }
                    for a in exp.ancestors
                ],
                "cross_references": [
                    {"id": str(x.id), "label": x.label} for x in exp.cross_references
                ],
                "definitions": [
                    {
                        "term": d.term,
                        "definition_provision_id": str(d.definition_provision_id),
                        "extracted_by": d.extracted_by,
                        "confidence": d.confidence,
                    }
                    for d in exp.definitions
                ],
            }
        )

    d = result.diagnostics
    return {
        "query": req.query,
        "refused": result.refused,
        # `flagged`: results are shown but low-confidence (the rerank was skipped, so there is
        # no calibrated score). The frontend renders a caution banner.
        "flagged": result.flagged,
        "reason": result.refusal_reason if (result.refused or result.flagged) else None,
        "top_rerank_score": result.top_rerank_score,
        # Which refusal threshold was applied and where it came from ("calibrated" for the
        # published fit, "fallback" for the 0.6 placeholder).
        "refusal_threshold": result.refusal_threshold,
        "refusal_source": result.refusal_source,
        # When refused, the lawyer view hides the results, but they are still included so an
        # operator can inspect what the system would have returned. The frontend renders the
        # refusal UI whenever `refused=true`.
        "results": results_out,
        "expanded": expanded_out,
        "diagnostics": {
            "variants": d.variants,
            "hypotheticals": d.hypotheticals,
            "n_result_sets": d.n_result_sets,
            "n_unique_candidates": d.n_unique_candidates,
            "n_fused": d.n_fused,
            "n_reranked": d.n_reranked,
            "rerank_skipped": d.rerank_skipped,
            "latency_ms": d.latency_ms,
            "latency_breakdown_ms": d.latency_breakdown_ms,
        },
    }
