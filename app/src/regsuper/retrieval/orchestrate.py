"""End-to-end retrieval: rewrite, HyDE, hybrid search, RRF, rerank, expansion and refusal.

This orchestrator is not on the live analysis path: the consumer cascade
(`analysis/l1_candidates.py`) is a pure metadata SQL query and never calls it. The callers are
the `/internal/retrieve` debug endpoint, the calibration materialiser (`calibration/pooled.py`),
`retrieve_ids` and a NotImplementedError Conduit stub. The calibrated refusal below therefore
does not gate end-user answers, and no compliance claim of the form "consumer answers below the
calibrated threshold are refused" holds for the live product. Wiring this orchestrator into the
cascade would be a separate, gated project.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from openai import AsyncOpenAI
from sqlalchemy import text as sql_text

from regsuper.analysis.llm_clients import get_openai
from regsuper.calibration.runtime import get_refusal_threshold
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_embedding
from regsuper.observability.events import emit
from regsuper.retrieval.cache import get_cached, make_key, put_cached
from regsuper.retrieval.expand import ExpandedProvision, expand_many
from regsuper.retrieval.hybrid import (
    RetrievalFilters,
    SearchHit,
    dense_search,
    sparse_search,
)
from regsuper.retrieval.hyde import hyde
from regsuper.retrieval.rerank import RerankedHit, rerank
from regsuper.retrieval.rewrite import rewrite
from regsuper.retrieval.rrf import FusedHit, reciprocal_rank_fusion
from regsuper.storage.database import session_scope

EMBED_MODEL = "text-embedding-3-large"
EMBED_DIM = 1536


@dataclass
class RetrieveDiagnostics:
    variants: list[str]
    hypotheticals: list[str]
    n_result_sets: int
    n_unique_candidates: int
    n_fused: int
    n_reranked: int
    rerank_skipped: bool
    latency_ms: int
    latency_breakdown_ms: dict[str, int]


@dataclass
class RetrievalResult:
    """Reranked candidates, their expansions, the fused RRF view and the refusal state.

    `candidates` is the reranked top-N and `expanded` their hierarchical expansions; `fused` is
    the pre-rerank RRF view, kept for debugging. Set `expand_top_n` to 0 to skip expansion and
    inspect the raw rerank output.

    The `refused` and `flagged` state applies only to the debug and calibration callers of this
    orchestrator (see the module docstring); it does not gate end-user answers.

    `refused` is True when the top rerank_score is below the calibrated refusal threshold
    (loaded at request time from `calibration/runtime.py`, falling back to
    `settings.refusal_threshold`); a caller should then surface an "I don't know" response
    instead of rendering the candidates. `flagged` is True when rerank was skipped, so no
    calibrated score exists: the answer is shown but marked low-confidence for human review,
    and must not be treated as a clean pass. `refusal_reason` carries the human-readable
    rationale for either state. `refusal_threshold` and `refusal_source` expose which threshold
    was applied and whether it came from the published calibration ("calibrated") or the
    placeholder ("fallback").
    """

    candidates: list[RerankedHit]
    expanded: list[ExpandedProvision]
    fused: list[FusedHit]
    diagnostics: RetrieveDiagnostics
    payloads: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    refused: bool = False
    flagged: bool = False
    refusal_reason: str | None = None
    refusal_threshold: float | None = None
    refusal_source: str | None = None
    top_rerank_score: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
EMBED_CACHE_KIND = "embed"


async def _embed_batch(
    client: AsyncOpenAI,
    texts: list[str],
    *,
    project_id: UUID | None,
    use_cache: bool = True,
) -> list[list[float]]:
    """Embed `texts` with caching.

    Each text gets its own row in `retrieval_cache` (kind='embed', key hashes
    the text + model + dim). Warm calls for the same text skip OpenAI entirely
    — embedding the user's verbatim query is the single biggest latency
    contributor on the warm path otherwise.
    """
    if not texts:
        return []

    keys: list[str] = []
    out: list[list[float] | None] = []
    missing_idx: list[int] = []
    missing_texts: list[str] = []

    for i, t in enumerate(texts):
        k = make_key(EMBED_CACHE_KIND, t, jurisdiction=EMBED_MODEL, sector=str(EMBED_DIM))
        keys.append(k)
        cached = await get_cached(k) if use_cache else None
        if isinstance(cached, list) and cached and isinstance(cached[0], (int, float)):
            out.append([float(x) for x in cached])
        else:
            out.append(None)
            missing_idx.append(i)
            missing_texts.append(t)

    if missing_texts:
        response = await tracked_embedding(
            client,
            source="retrieval.embed",
            project_id=project_id,
            model=EMBED_MODEL,
            input=missing_texts,
            dimensions=EMBED_DIM,
        )
        fresh = [list(d.embedding) for d in response.data]
        for slot, vec in zip(missing_idx, fresh, strict=True):
            out[slot] = vec
            if use_cache:
                # Embeddings are deterministic, so the TTL could be longer than for the
                # LLM caches; we reuse the 12-hour default to keep a single knob.
                await put_cached(keys[slot], kind=EMBED_CACHE_KIND, value=vec)

    # Every slot is filled by now; the filter only narrows the type for mypy.
    return [v for v in out if v is not None]


async def _search_one_hypothetical(
    *,
    embedding: list[float],
    text: str,
    filters: RetrievalFilters,
    k: int,
) -> list[list[SearchHit]]:
    dp, ds, sp, ss = await asyncio.gather(
        dense_search(embedding, k=k, filters=filters, level="provision"),
        dense_search(embedding, k=k, filters=filters, level="sentence"),
        sparse_search(text, k=k, filters=filters, level="provision"),
        sparse_search(text, k=k, filters=filters, level="sentence"),
    )
    return [dp, ds, sp, ss]


async def _load_documents(
    provision_ids: list[UUID],
) -> dict[UUID, dict[str, Any]]:
    """Fetch the text and a few metadata fields for the rerank input, in one round trip."""
    if not provision_ids:
        return {}
    sql = """
        SELECT p.id, p.label, p.text,
               sd.external_id, sd.title, p.legal_hierarchy, p.jurisdiction
          FROM provision p
          JOIN source_document sd ON sd.id = p.source_document_id
         WHERE p.id = ANY(:ids)
    """
    async with session_scope() as session:
        rows = (await session.execute(sql_text(sql), {"ids": provision_ids})).all()
    return {
        r[0]: {
            "label": r[1],
            "text": r[2],
            "external_id": r[3],
            "title": r[4],
            "legal_hierarchy": r[5],
            "jurisdiction": r[6],
        }
        for r in rows
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def retrieve(
    query: str,
    *,
    jurisdiction: str = "",
    sector: str = "",
    filters: RetrievalFilters | None = None,
    project_id: UUID | None = None,
    k_per_leg: int = 30,
    # Number of RRF candidates fed to the reranker. 30 is what the pipeline runs with
    # (calibration materialisation and any caller that omits the argument); the
    # `/internal/retrieve` debug endpoint and the rerank module use the same number.
    top_n_rrf: int = 30,
    top_n_rerank: int = 10,
    expand_top_n: int = 10,
    max_hyde_variants: int = 1,
    use_cache: bool = True,
) -> RetrievalResult:
    """Run the full retrieval pipeline for `query`.

    Steps:
      1. rewrite() the query into variants;
      2. hyde() on up to `max_hyde_variants` of the rewritten variants;
      3. embed the verbatim query and each hypothetical;
      4. for each search text, dense and sparse search at provision and sentence level;
      5. RRF merge over all result sets, keeping the top `top_n_rrf`;
      6. Cohere rerank of the fused candidates down to `top_n_rerank`;
      7. hierarchical expand() of the top `expand_top_n` reranked hits;
      8. telemetry;
      9. calibrated refusal, or a low-confidence flag when rerank was skipped.

    Rerank degrades gracefully when COHERE_API_KEY is unset (the RRF order is kept with
    rerank_score=None). Expansion is on by default because it costs about 10 ms per hit and
    the parent context is always useful.

    Acceptance targets: a canonical query has AIFMD Article 31 or 32 in its top 5; a negative
    query's top result has rerank_score below 0.5; end-to-end latency stays under 2.5 s on a
    CX42-class VPS.
    """
    filters = filters or RetrievalFilters()
    project_str = str(project_id) if project_id else None
    breakdown: dict[str, int] = {}
    t_start = time.perf_counter()

    # ---- Step 1: query rewriting --------------------------------------
    await emit("retrieval.rewrite.start", project_id=project_str)
    t = time.perf_counter()
    variants = await rewrite(query, jurisdiction=jurisdiction, sector=sector, use_cache=use_cache)
    # Drop the literal original from the HyDE feed: a separate verbatim search path below
    # keeps HyDE from language-biasing the user's exact phrasing away. (Observed failure: a
    # query mentioning "CNB" produced Czech hypotheticals, and the English AIFMD text never
    # entered the candidate pool.)
    # `max_hyde_variants` caps the search fan-out: Postgres HNSW probes contend for CPU above
    # about 8 concurrent queries, so one HyDE text plus the verbatim query (8 legs in total) is
    # the sweet spot at the current scale.
    hyde_inputs = [v for v in variants if v.strip() and v.strip() != query.strip()]
    hyde_inputs = hyde_inputs[: max(0, int(max_hyde_variants))]
    breakdown["rewrite_ms"] = int((time.perf_counter() - t) * 1000)
    await emit(
        "retrieval.rewrite.done",
        project_id=project_str,
        variants=len(hyde_inputs),
        latency_ms=breakdown["rewrite_ms"],
    )

    # ---- Step 2: HyDE on the rewritten variants only ------------------
    await emit("retrieval.hyde.start", project_id=project_str, variants=len(hyde_inputs))
    t = time.perf_counter()
    hyde_texts: list[str] = await asyncio.gather(
        *(
            hyde(v, jurisdiction=jurisdiction, sector=sector, use_cache=use_cache)
            for v in hyde_inputs
        )
    )
    hyde_texts = [h for h in hyde_texts if h and h.strip()]
    breakdown["hyde_ms"] = int((time.perf_counter() - t) * 1000)
    await emit(
        "retrieval.hyde.done",
        project_id=project_str,
        variants=len(hyde_texts),
        latency_ms=breakdown["hyde_ms"],
    )

    # ---- Step 3: build the full search-text set & embed everything ----
    # The search texts are the user's raw query (verbatim, no HyDE transform) plus each HyDE
    # hypothetical. The raw-query path is what rescues English source documents when HyDE
    # generates hypotheticals in the target language.
    search_texts: list[str] = [query, *hyde_texts]
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    t = time.perf_counter()
    embeddings = await _embed_batch(
        client, search_texts, project_id=project_id, use_cache=use_cache
    )
    breakdown["embed_ms"] = int((time.perf_counter() - t) * 1000)

    # ---- Step 4: hybrid search per search-text ------------------------
    t = time.perf_counter()
    nested = await asyncio.gather(
        *(
            _search_one_hypothetical(
                embedding=emb,
                text=text,
                filters=filters,
                k=k_per_leg,
            )
            for emb, text in zip(embeddings, search_texts, strict=True)
        )
    )
    result_sets: list[list[SearchHit]] = [s for legs in nested for s in legs]
    breakdown["search_ms"] = int((time.perf_counter() - t) * 1000)

    # ---- Step 5: RRF merge --------------------------------------------
    t = time.perf_counter()
    fused = reciprocal_rank_fusion(result_sets, k=60, top_n=top_n_rrf)
    breakdown["rrf_ms"] = int((time.perf_counter() - t) * 1000)

    await emit(
        "retrieval.search.done",
        project_id=project_str,
        n_indexes=len(result_sets),
        n_candidates=len(fused),
    )

    # ---- Step 6: Cohere rerank ---------------------------------------
    t = time.perf_counter()
    documents = await _load_documents([h.provision_id for h in fused])
    rerank_input: list[tuple[UUID, str, float, dict[str, Any] | None]] = []
    for h in fused:
        meta = documents.get(h.provision_id)
        if meta is None:
            continue
        rerank_input.append((h.provision_id, meta["text"], h.score, meta))
    reranked = await rerank(
        query=query,
        candidates=rerank_input,
        top_n=top_n_rerank,
        project_id=project_str,
    )
    breakdown["rerank_ms"] = int((time.perf_counter() - t) * 1000)
    rerank_skipped = bool(reranked) and reranked[0].rerank_score is None

    # ---- Step 7: hierarchical expansion -------------------------------
    expanded: list[ExpandedProvision] = []
    if expand_top_n > 0 and reranked:
        t = time.perf_counter()
        target_ids = [r.provision_id for r in reranked[:expand_top_n]]
        expanded = await expand_many(target_ids)
        breakdown["expand_ms"] = int((time.perf_counter() - t) * 1000)
        await emit(
            "retrieval.expand.done",
            project_id=project_str,
            n=len(expanded),
        )
    else:
        breakdown["expand_ms"] = 0

    # ---- Step 8: telemetry --------------------------------------------
    unique_ids = {h.provision_id for s in result_sets for h in s}
    total_ms = int((time.perf_counter() - t_start) * 1000)
    breakdown["total_ms"] = total_ms

    top_score: float = 0.0
    if reranked and reranked[0].rerank_score is not None:
        top_score = reranked[0].rerank_score
    elif fused:
        top_score = fused[0].score
    await emit(
        "retrieval.candidates.found",
        project_id=project_str,
        n=len(reranked),
        top_score=round(top_score, 5),
    )

    # ---- Step 9: calibrated refusal -------------------------------------
    # The threshold is loaded at request time from the persisted calibration
    # (calibration/runtime.py), falling back to the documented 0.6 placeholder in config.py
    # when no fit has been published. `source` tells the operator which one is live, so a
    # stale fallback is visible rather than silent.
    # The comparison uses the cross-encoder rerank_score. Without a Cohere key the rerank is
    # skipped (rerank_score=None), so results are only ever flagged here, never refused; and
    # no calibrated threshold can be fitted (every eval row would have rerank_score=None), so
    # the fallback stays live. Publish a fit with `python -m regsuper.calibration.runtime
    # publish` once a Cohere key and labels exist. This logic governs only the debug and
    # calibration path (see the module docstring).
    refused = False
    flagged = False
    refusal_reason: str | None = None
    rt = get_refusal_threshold()
    refusal_threshold = rt.value
    if reranked and reranked[0].rerank_score is not None:
        # Normal path: a real cross-encoder score we can compare to the threshold.
        if reranked[0].rerank_score < refusal_threshold:
            refused = True
            refusal_reason = (
                f"top rerank_score {reranked[0].rerank_score:.3f} below "
                f"{rt.source} threshold {refusal_threshold:.3f} — insufficient "
                f"retrieval confidence"
            )
            await emit(
                "retrieval.refused",
                project_id=project_str,
                level="warn",
                score=round(reranked[0].rerank_score, 4),
                threshold=round(refusal_threshold, 4),
                source=rt.source,
            )
    elif reranked:
        # Rerank was skipped (Cohere key missing or rate-limited), so there is no calibrated
        # cross-encoder score and the answer must not pass as confident. The fused RRF score is
        # on a different scale from the rerank threshold, so the two cannot be compared;
        # instead the result is flagged as uncalibrated and low-confidence. The caller (and the
        # lawyer UI) treat a flagged result as "answer with a visible caution and human
        # review", never as a clean pass.
        flagged = True
        refusal_reason = (
            "rerank skipped (no cross-encoder score) — retrieval confidence is "
            "uncalibrated; treat results as low-confidence pending human review"
        )
        await emit(
            "retrieval.flagged",
            project_id=project_str,
            level="warn",
            reason="rerank_skipped",
            top_rrf_score=round(reranked[0].rrf_score, 4),
        )

    return RetrievalResult(
        candidates=reranked,
        expanded=expanded,
        fused=fused,
        diagnostics=RetrieveDiagnostics(
            variants=[query, *hyde_inputs],
            hypotheticals=hyde_texts,
            n_result_sets=len(result_sets),
            n_unique_candidates=len(unique_ids),
            n_fused=len(fused),
            n_reranked=len(reranked),
            rerank_skipped=rerank_skipped,
            latency_ms=total_ms,
            latency_breakdown_ms=breakdown,
        ),
        payloads=documents,
        refused=refused,
        flagged=flagged,
        refusal_reason=refusal_reason,
        refusal_threshold=refusal_threshold,
        refusal_source=rt.source,
        top_rerank_score=(
            reranked[0].rerank_score if reranked and reranked[0].rerank_score is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Convenience for callers that just want IDs
# ---------------------------------------------------------------------------
async def retrieve_ids(
    query: str,
    **kwargs: Any,
) -> list[UUID]:
    result = await retrieve(query, **kwargs)
    return [h.provision_id for h in result.candidates]
