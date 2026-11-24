"""Hybrid retrieval primitives: dense, sparse and metadata-filtered search.

These make up the candidate stage of the retrieval pipeline:

  - dense_search(embedding, k, filters, level): cosine-similarity hits
  - sparse_search(text, k, filters, level): BM25-style full-text (tsv) hits
  - metadata_filter_ids(filters): a pure WHERE-clause filter, no scoring

The searches return `list[SearchHit]` keyed at the provision level (sentence-level searches
roll up to their parent provision with the max score), so RRF fusion can merge result sets
without a level mismatch.

Filters honoured (see `metadata_filter_sql`):
  - jurisdictions:   provision.jurisdiction = ANY(...)
  - sector_tags:     provision.sector_tags && ... (array overlap)
  - legal_hierarchy: provision.legal_hierarchy = ANY(...)
  - languages:       provision.language = ANY(...)
  - as_of:           in force on that date (valid_from <= as_of < valid_to)
  - active_only:     valid_to IS NULL OR valid_to > CURRENT_DATE (ignored when as_of is set)

Distance metric: pgvector's `<=>` is cosine distance (0 means identical), so similarity is
1 - distance. We return similarity so that larger is better and the RRF stage does not need to
know which sign convention each search used.

Sparse-query strategy: `websearch_to_tsquery` ANDs every token (stopwords included), which
yields zero hits for natural-language questions. We tokenise client-side, drop stopwords and
tokens shorter than three characters, and OR the rest through `to_tsquery`. ts_rank_cd then
ranks by term-frequency overlap, giving the BM25-like recall we want from the sparse leg of a
hybrid system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import text

from regsuper.storage.database import session_scope

# Stopwords for tokenisation — light EN + CZ list covering the conversational
# words that bloat natural-language queries. The Postgres `simple` config has
# no stopword list of its own (that's the whole point of using `simple`).
_STOPWORDS: frozenset[str] = frozenset(
    # English
    "a an and are as at be but by do does for from has have how i if "
    "in into is it its many much my no not of on or our shall should so "
    "such that the their there these they this those to was we were what "
    "when where which who why will with would you your".split()
    # Czech
    + "a aby ale ani aniž až co či do je jeho jejich jest ji jí jsem jsi "
    "jsme jsou jste k kde kdo když ke ku má mé mí mít mu my na nad nás "
    "naše ne nebo nechť nejen není než ní nic od pak po pod podle pro "
    "proč proto protože při před přes přese se si svá své svůj ta tady "
    "tak také takto te ten tě těch ti to tu tvá tvé tvůj ty u v ve "
    "však vy vás váš všechen z za ze ž že".split()
)


def _tokenise_for_tsquery(query: str) -> str:
    """Turn a free-form query into an OR-joined tsquery body.

    Returns an empty string if no token survives; callers must short-circuit on an empty
    result so Postgres is never handed an invalid `to_tsquery('')`.
    """
    if not query:
        return ""
    # Strip punctuation, split on any non-word char (Unicode aware so Czech
    # diacritics stay intact).
    raw = re.split(r"\W+", query.lower(), flags=re.UNICODE)
    keep = [t for t in raw if len(t) >= 3 and t not in _STOPWORDS]
    return " | ".join(keep)


SearchLevel = Literal["provision", "sentence"]


@dataclass(frozen=True)
class SearchHit:
    """One retrieval candidate.

    `score` is comparable within a single result set, not across kinds; RRF later converts
    ranks to a common scale.
    """

    provision_id: UUID
    score: float
    source: str  # 'dense.provision', 'sparse.sentence', etc.
    sentence_id: UUID | None = None


@dataclass
class RetrievalFilters:
    """Metadata constraints applied to every candidate search."""

    jurisdictions: list[str] = field(default_factory=list)
    sector_tags: list[str] = field(default_factory=list)
    legal_hierarchy: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    active_only: bool = True
    # Point-in-time query: when set, return the law as it stood on this date. as_of owns the
    # temporal window and supersedes active_only (see metadata_filter_sql), so the two never
    # double-filter. None means active_only governs.
    as_of: date | None = None


# ---------------------------------------------------------------------------
# Filter SQL composition
# ---------------------------------------------------------------------------
def metadata_filter_sql(
    filters: RetrievalFilters,
    *,
    alias: str = "p",
) -> tuple[str, dict[str, object]]:
    """Render the metadata WHERE fragment plus its bind params.

    Returned as `("AND clause1 AND clause2", {param: value})` so callers can
    splice into a parent SELECT regardless of what's in the FROM list.
    Empty / vacuous filters yield an empty string and `{}`.
    """
    clauses: list[str] = []
    params: dict[str, object] = {}

    if filters.jurisdictions:
        clauses.append(f"{alias}.jurisdiction = ANY(:f_jurisdictions)")
        params["f_jurisdictions"] = list(filters.jurisdictions)
    if filters.legal_hierarchy:
        clauses.append(f"{alias}.legal_hierarchy = ANY(:f_legal_hierarchy)")
        params["f_legal_hierarchy"] = list(filters.legal_hierarchy)
    if filters.languages:
        clauses.append(f"{alias}.language = ANY(:f_languages)")
        params["f_languages"] = list(filters.languages)
    if filters.sector_tags:
        # provision.sector_tags is text[]; && is array overlap.
        clauses.append(f"{alias}.sector_tags && :f_sector_tags")
        params["f_sector_tags"] = list(filters.sector_tags)
    if filters.as_of is not None:
        # Point in time: as_of owns the whole temporal window and replaces active_only's
        # CURRENT_DATE clause (otherwise the two would double-filter and over-exclude).
        # valid_from and valid_to are nullable; a NULL bound is open on that side.
        clauses.append(
            f"({alias}.valid_from IS NULL OR {alias}.valid_from <= :f_as_of) "
            f"AND ({alias}.valid_to IS NULL OR {alias}.valid_to > :f_as_of)"
        )
        params["f_as_of"] = filters.as_of
    elif filters.active_only:
        clauses.append(f"({alias}.valid_to IS NULL OR {alias}.valid_to > CURRENT_DATE)")

    if not clauses:
        return "", {}
    return " AND " + " AND ".join(clauses), params


async def metadata_filter_ids(
    filters: RetrievalFilters,
    *,
    limit: int | None = None,
) -> list[UUID]:
    """Return provision IDs that pass the metadata filter (no scoring).

    Useful when an analysis pass wants the universe of eligible provisions for a project,
    e.g. the L1 "wide net" candidate stage. With no filters set, returns every provision
    (bounded by `limit` if supplied).
    """
    where, params = metadata_filter_sql(filters)
    sql = f"SELECT id FROM provision p WHERE 1=1 {where}"
    if limit is not None:
        sql += " LIMIT :_limit"
        params["_limit"] = int(limit)
    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).all()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Dense (HNSW) search
# ---------------------------------------------------------------------------

# HNSW search-time knob. The pgvector default is 40; we use 20 because:
#   - the orchestrator runs one dense leg per search text and level, several in parallel;
#   - each leg only needs the top 50, and the reranker compensates for any small recall
#     loss at the leaf;
#   - ef_search = 20 cut per-query latency by about 50% in our smoke runs.
# Override per call with `dense_search(..., ef_search=N)` when a use case (e.g. an
# exhaustive eval) needs higher recall.
DEFAULT_HNSW_EF_SEARCH = 20


async def dense_search(
    embedding: list[float],
    *,
    k: int = 50,
    filters: RetrievalFilters | None = None,
    level: SearchLevel = "provision",
    ef_search: int = DEFAULT_HNSW_EF_SEARCH,
) -> list[SearchHit]:
    """HNSW-backed cosine similarity search.

    At `level='provision'`, queries `provision.embedding_semantic` directly.
    At `level='sentence'`, queries `provision_sentence.embedding` and rolls
    up to provision_id (keeping the max sentence-similarity per provision)
    so downstream RRF sees provision-keyed hits regardless of granularity.
    """
    filters = filters or RetrievalFilters()
    if len(embedding) == 0:
        return []
    qvec = _vector_literal(embedding)
    ef = max(1, int(ef_search))

    if level == "provision":
        where, params = metadata_filter_sql(filters)
        sql = f"""
            SELECT p.id,
                   1 - (p.embedding_semantic <=> CAST(:q AS vector)) AS sim
              FROM provision p
             WHERE p.embedding_semantic IS NOT NULL
               {where}
             ORDER BY p.embedding_semantic <=> CAST(:q AS vector)
             LIMIT :k
        """
        params.update({"q": qvec, "k": int(k)})
        async with session_scope() as session:
            # asyncpg cannot prepare multi-statement strings, so SET LOCAL runs as its own
            # execute, followed by the SELECT inside the same transaction.
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
            rows = (await session.execute(text(sql), params)).all()
        return [
            SearchHit(provision_id=r[0], score=float(r[1]), source="dense.provision") for r in rows
        ]

    # Sentence level: over-fetch, then aggregate to provision granularity. k + 20 is enough at
    # the current corpus size: with ef_search = 20 the HNSW graph returns roughly that many
    # candidates per probe, so larger fetches mostly fill with duplicates from neighbouring
    # provisions.
    fetch = min(max(k + 20, k + 10), 200)
    where, params = metadata_filter_sql(filters)
    sql = f"""
        WITH ranked AS (
            SELECT ps.id AS sentence_id,
                   ps.provision_id,
                   1 - (ps.embedding <=> CAST(:q AS vector)) AS sim
              FROM provision_sentence ps
              JOIN provision p ON p.id = ps.provision_id
             WHERE ps.embedding IS NOT NULL
               {where}
             ORDER BY ps.embedding <=> CAST(:q AS vector)
             LIMIT :fetch
        ),
        rolled AS (
            SELECT provision_id,
                   MAX(sim)            AS sim,
                   (ARRAY_AGG(sentence_id ORDER BY sim DESC))[1] AS best_sentence
              FROM ranked
             GROUP BY provision_id
        )
        SELECT provision_id, sim, best_sentence
          FROM rolled
         ORDER BY sim DESC
         LIMIT :k
    """
    params.update({"q": qvec, "k": int(k), "fetch": fetch})
    async with session_scope() as session:
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
        rows = (await session.execute(text(sql), params)).all()
    return [
        SearchHit(
            provision_id=r[0],
            score=float(r[1]),
            source="dense.sentence",
            sentence_id=r[2],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Sparse (full-text) search
# ---------------------------------------------------------------------------
async def sparse_search(
    query_text: str,
    *,
    k: int = 50,
    filters: RetrievalFilters | None = None,
    level: SearchLevel = "provision",
) -> list[SearchHit]:
    """BM25-style sparse retrieval over the existing tsv columns.

    Tokenises the input client-side (dropping stopwords and short tokens), OR-joins the
    survivors, and passes the result to `to_tsquery('simple', :q)`. Using `simple` keeps the
    queries on the pre-built GIN indexes (`ix_p_tsv`, `ix_ps_tsv`); `english` would silently
    bypass them.

    Sentence-level rolls up to provision the same way as dense_search.
    """
    filters = filters or RetrievalFilters()
    q_body = _tokenise_for_tsquery(query_text)
    if not q_body:
        return []

    if level == "provision":
        where, params = metadata_filter_sql(filters)
        sql = f"""
            SELECT p.id,
                   ts_rank_cd(p.tsv, to_tsquery('simple', :q)) AS rank
              FROM provision p
             WHERE p.tsv @@ to_tsquery('simple', :q)
               {where}
             ORDER BY rank DESC
             LIMIT :k
        """
        params.update({"q": q_body, "k": int(k)})
        async with session_scope() as session:
            rows = (await session.execute(text(sql), params)).all()
        return [
            SearchHit(provision_id=r[0], score=float(r[1]), source="sparse.provision") for r in rows
        ]

    fetch = min(max(k * 4, k + 10), 500)
    where, params = metadata_filter_sql(filters)
    sql = f"""
        WITH ranked AS (
            SELECT ps.id AS sentence_id,
                   ps.provision_id,
                   ts_rank_cd(ps.tsv, to_tsquery('simple', :q)) AS rank
              FROM provision_sentence ps
              JOIN provision p ON p.id = ps.provision_id
             WHERE ps.tsv @@ to_tsquery('simple', :q)
               {where}
             ORDER BY rank DESC
             LIMIT :fetch
        ),
        rolled AS (
            SELECT provision_id,
                   MAX(rank)                                AS rank,
                   (ARRAY_AGG(sentence_id ORDER BY rank DESC))[1] AS best_sentence
              FROM ranked
             GROUP BY provision_id
        )
        SELECT provision_id, rank, best_sentence
          FROM rolled
         ORDER BY rank DESC
         LIMIT :k
    """
    params.update({"q": q_body, "k": int(k), "fetch": fetch})
    async with session_scope() as session:
        rows = (await session.execute(text(sql), params)).all()
    return [
        SearchHit(
            provision_id=r[0],
            score=float(r[1]),
            source="sparse.sentence",
            sentence_id=r[2],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _vector_literal(vec: list[float]) -> str:
    """Format a vector as the pgvector string literal `[1.0,2.0,...]`.

    Every query above binds it through `CAST(:q AS vector)`. Formatting in Python means the
    bound parameter does not round-trip through asyncpg's vector type registration, which is
    fussy about session state.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
