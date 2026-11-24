"""Cross-encoder reranking with Cohere Rerank v3.5.

The first-stage legs (dense bi-encoder and sparse full-text) score query and passage in
isolation; a cross-encoder sees both at once and tells `may` from `shall`, or `applies` from
`does not apply`, in ways embeddings cannot. The Cohere model is multilingual (Czech included),
so it can score an English query against a Czech transposition. That is what pulls the English
AIFMD text back to the top of canonical queries when HyDE drifts into Czech.

Every Cohere call goes through `tracked_cohere_rerank`, so the kill switch and the daily and
monthly spend caps gate it, and its cost lands in `api_call` for the admin dashboard. When
`COHERE_API_KEY` is unset (or Cohere rate-limits us), the input slice is returned in order with
`rerank_score=None` and `retrieval.rerank.skipped` is emitted; callers must treat None as
"no rerank applied" before thresholding.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import cohere
from cohere.errors import TooManyRequestsError

from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_cohere_rerank
from regsuper.observability.events import emit

RERANK_MODEL = "rerank-v3.5"

# Cohere caps a request at 1000 documents. The orchestrator only feeds the top-N RRF
# candidates (default 30, see `top_n_rrf` in orchestrate.retrieve), so this ceiling of 200 is
# a defensive limit for callers that request a larger fan-out.
MAX_DOCS_PER_CALL = 200


@dataclass(frozen=True)
class RerankedHit:
    """One re-scored candidate.

    `rerank_score` is None when no rerank was applied (Cohere key missing or rate-limited);
    callers must check before thresholding.
    """

    provision_id: UUID
    rerank_score: float | None
    rrf_score: float
    document: str
    payload: dict[str, Any] | None = None


def _client() -> cohere.AsyncClientV2 | None:
    settings = get_settings()
    if not settings.cohere_api_key:
        return None
    return cohere.AsyncClientV2(api_key=settings.cohere_api_key)


async def rerank(
    *,
    query: str,
    candidates: list[tuple[UUID, str, float, dict[str, Any] | None]],
    top_n: int = 10,
    project_id: str | None = None,
) -> list[RerankedHit]:
    """Re-score `(provision_id, text, rrf_score, payload)` tuples against `query`.

    Returns the top-N candidates sorted by `rerank_score` descending. If no
    Cohere key is configured, returns the first `top_n` candidates in input
    order with `rerank_score=None`.
    """
    if not candidates:
        return []

    client = _client()
    if client is None:
        await emit(
            "retrieval.rerank.skipped",
            project_id=project_id,
            reason="COHERE_API_KEY not configured",
            level="warn",
        )
        return [
            RerankedHit(
                provision_id=pid,
                rerank_score=None,
                rrf_score=rrf,
                document=doc,
                payload=payload,
            )
            for pid, doc, rrf, payload in candidates[:top_n]
        ]

    documents = [doc for _, doc, _, _ in candidates[:MAX_DOCS_PER_CALL]]

    t0 = time.perf_counter()
    try:
        response = await tracked_cohere_rerank(
            client,
            source="retrieval.rerank",
            project_id=UUID(project_id) if project_id else None,
            model=RERANK_MODEL,
            query=query,
            documents=documents,
            top_n=min(top_n, len(documents)),
        )
    except TooManyRequestsError as exc:
        # Trial keys are limited to 10 requests per minute, production keys to about 10k.
        # Fall back to the unreranked order so the request still returns a useful answer.
        await emit(
            "retrieval.rerank.skipped",
            project_id=project_id,
            reason=f"Cohere 429 rate-limited: {str(exc)[:200]}",
            level="warn",
        )
        return [
            RerankedHit(
                provision_id=pid,
                rerank_score=None,
                rrf_score=rrf,
                document=doc,
                payload=payload,
            )
            for pid, doc, rrf, payload in candidates[:top_n]
        ]
    latency_ms = int((time.perf_counter() - t0) * 1000)

    ranked: list[RerankedHit] = []
    for result in response.results:
        idx = result.index
        pid, doc, rrf, payload = candidates[idx]
        ranked.append(
            RerankedHit(
                provision_id=pid,
                rerank_score=float(result.relevance_score),
                rrf_score=rrf,
                document=doc,
                payload=payload,
            )
        )

    await emit(
        "retrieval.rerank.done",
        project_id=project_id,
        n=len(ranked),
        latency_ms=latency_ms,
        top_score=round(ranked[0].rerank_score, 4) if ranked else 0.0,
    )
    return ranked
