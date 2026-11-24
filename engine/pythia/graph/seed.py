"""Seed retrieval: turn a natural-language query into the entry provisions for graph expansion.

Three paths, all running locally without API keys:
  - sparse_seed: PostgreSQL full-text search over provision.tsv (lexical).
  - dense_seed: local multilingual embedding of the query, ANN search over a provision embedding
    column (pgvector cosine). Matches by meaning and across languages (Czech query, EU text).
  - hybrid_seed: sparse and dense fused by reciprocal-rank fusion (the engine default). Falls back
    to sparse alone if embeddings have not been built.
All apply the in-force mask (valid_from/valid_to) and an optional jurisdiction filter.

Run:  .venv/bin/python -m pythia.graph.seed "your query"
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from datetime import date

from pythia.config import get_settings
from pythia.db import close_pool, get_pool


@dataclass
class Seed:
    provision_id: str
    score: float
    # The text of the version in force at a past as_of: the provision_version row whose
    # [valid_from, valid_to) window contains as_of, not the head text. Set only when
    # temporal_substrate_enabled is on and as_of is strictly in the past; otherwise None.
    # engine.retrieve() copies it onto the seed's NeighborHit, and engine._decorate serves it as
    # CitedProvision.text_md when temporal_asof_render_enabled is on. Both flags default to off and
    # are independent.
    asof_text: str | None = None


DEFAULT_EMBEDDER = "bge"  # bge-m3 beat mpnet in evaluation (dense R@10 0.378 to 0.511)

# The cross-encoder rerank candidate pool. A pool of 50 took about 95% of warm engine latency
# (around 3.4 s) for a result truncated to k of about 8. A pool of 16 measured the same grounded
# recall (R@8 0.487 vs 0.475 at 50, R@10 0.586 vs 0.590) at around 1.34 s: a 2.5x end-to-end
# speed-up with no recall loss. 16 is the floor (a pool of 8 lost about 5 recall points); larger
# requests scale with k. Re-validate with eval/run_eval.py after tuning.
_RERANK_POOL = 16


# --------------------------------------------------------------------------- IDF-ranked seed tokens
# Controlled by seed_token_idf_enabled (default off). By default sparse_seed keeps the first 14
# query tokens of at least 3 characters. For a long, vague layman question that positional slice can
# drop the one discriminative legal term. With the flag on, the tokens are ranked by inverse
# document frequency instead, so rare, discriminative terms win over common ones. Document
# frequencies come from an in-process cache built from provision.tsv with ts_stat (per-lexeme
# document counts). Ties keep the original query order (stable sort), so the result is
# deterministic.
#
# The cache is a process-global {lexeme: ndoc} dict, filled once on first use (one ts_stat pull,
# ~58k rows, under 0.5 s). provision.tsv is to_tsvector('czech', text), so its keys are
# hunspell-stemmed Czech lexemes, not raw tokens.
#
# Each query token is therefore normalised to its 'czech' lexeme before the lookup. Looking up raw
# tokens inverts the ranking: an inflected common word ("zaměstnance") misses its lexeme
# ("zaměstnanec", df 2145, the most common term), gets df 0, looks rare and wins the seed slots over
# genuinely discriminative terms ("restauraci", lexeme "restaurace", df 5). A token whose lexeme is
# still absent after normalisation (a true out-of-vocabulary word, or a stopword with no lexeme) is
# unknown and ranks below every term found in the corpus: it cannot produce a tsquery match, so it
# must never outrank a term that can.
_DF_CACHE: dict[str, int] | None = None
_DF_TOTAL: int = 0


async def _load_df_cache() -> tuple[dict[str, int], int]:
    """Load the process-global document-frequency cache from provision.tsv on first use.

    Returns `(df_by_lexeme, n_docs)`, where df is the number of provisions each lexeme occurs in.
    One read-only ts_stat pull; later calls return the cached values.
    """
    global _DF_CACHE, _DF_TOTAL
    if _DF_CACHE is not None:
        return _DF_CACHE, _DF_TOTAL
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT word, ndoc FROM ts_stat('SELECT tsv FROM provision WHERE tsv IS NOT NULL')")
        total = await conn.fetchval("SELECT count(*) FROM provision WHERE tsv IS NOT NULL") or 0
    _DF_CACHE = {r["word"]: int(r["ndoc"]) for r in rows}
    _DF_TOTAL = int(total)
    return _DF_CACHE, _DF_TOTAL


async def _lexemes_for(terms: list[str]) -> dict[str, list[str]]:
    """Map each query token to its lexemes under the 'czech' text-search config.

    This is the lexeme space the df cache is keyed in (provision.tsv is to_tsvector('czech', text)),
    so df lookups use these lexemes rather than raw tokens. One batched read-only query. A stopword
    or punctuation-only token maps to an empty list.
    """
    uniq = list(dict.fromkeys(terms))  # order-preserving de-duplication (one row per distinct token)
    if not uniq:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tok, tsvector_to_array(to_tsvector('czech', tok)) AS lex FROM unnest($1::text[]) AS tok",
            uniq,
        )
    return {r["tok"]: list(r["lex"] or []) for r in rows}


def _idf_rank(
    terms: list[str],
    df: dict[str, int],
    n_docs: int,
    *,
    lex_map: dict[str, list[str]] | None = None,
    limit: int = 14,
) -> list[str]:
    """Select the `limit` rarest terms, ranked by ascending document frequency (pure, no database).

    `terms` are already filtered to at least 3 characters and in query order; ties keep that order
    via a stable sort on (df, original index). A token's df is the lowest df among its 'czech'
    lexemes from `lex_map`; without `lex_map` each token is its own lexeme (used by the pure unit
    tests). A token whose lexemes are absent from the corpus, or that has none, is unknown: its key
    is strictly greater than any real df, so it ranks below every term found in the corpus instead
    of looking rare. `n_docs` caps a real df.
    """
    n = max(n_docs, 1)
    unknown_key = float(n) + 1.0  # strictly above any capped real df, so unknown tokens sort last

    def _key(item: tuple[int, str]) -> tuple[float, int]:
        idx, t = item
        lexemes = lex_map.get(t) if lex_map is not None else None
        if lexemes is None:  # no normalisation available: the token is its own lexeme
            lexemes = [t.lower()]
        present = [df[lx] for lx in lexemes if lx in df]
        if not present:  # out of vocabulary or stopword-only: ranks below any corpus term
            return (unknown_key, idx)
        return (float(min(min(present), n)), idx)  # rarest matching lexeme; capped at n

    ranked = sorted(enumerate(terms), key=_key)
    return [t for _, t in ranked[:limit]]


async def _seed_terms(query: str, *, idf: bool) -> list[str]:
    """Select the query tokens sparse_seed searches for, at most 14 so the tsquery stays bounded.

    Tokens are `\\w+` runs of at least 3 characters. With `idf=False` the first 14 in query order
    are kept; with `idf=True` they are re-ordered by IDF over their 'czech' lexemes before
    truncation, so rare, discriminative terms are kept and out-of-vocabulary filler is dropped.
    """
    terms = [t for t in re.findall(r"\w+", query, re.UNICODE) if len(t) >= 3]
    if not idf:
        return terms[:14]
    df, n_docs = await _load_df_cache()
    lex_map = await _lexemes_for(terms)
    return _idf_rank(terms, df, n_docs, lex_map=lex_map, limit=14)


# --------------------------------------------------------------------------- current_only filter
# The single source of truth for the current_only point-in-time status predicate, shared by
# ports.candidates, seed, cte_expand.four_corners and ppr (on both edge endpoints). It fails open:
# rows that are IN_FORCE or NULL are kept (NULL means no version lineage, i.e. unknown, and real
# current law without lineage must never be lost); only rows known not to be current (SUPERSEDED,
# NOT_YET_IN_FORCE) are excluded. The predicate is templated on the provision alias so the same
# logic serves every site: `p` (candidates, seed, the four-corners final SELECT) and PPR's `ps`/`pd`
# edge endpoints. With the flag off it is "".
_CURRENT_ONLY_PRED_TMPL = "AND ({a}.pit_status IS NULL OR {a}.pit_status = 'IN_FORCE')"
_CURRENT_ONLY_PRED = _CURRENT_ONLY_PRED_TMPL.format(a="p")  # the p-aliased form (candidates, seed, cte)


async def _pit_status_col_present(conn) -> bool:
    """Return whether provision.pit_status exists, the only column current_only needs.

    A database without the column falls back to the unfiltered query.
    """
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='provision' AND column_name='pit_status')"
        )
    )


async def current_only_clause(conn, *, aliases: tuple[str, ...] = ("p",)) -> str:
    """Return the current_only predicates as a WHERE fragment, or "" when the filter is inactive.

    The filter is active only when config.temporal_current_only_enabled is on and
    provision.pit_status exists. One predicate is emitted per entry in `aliases`, each on its own
    indented line; the default ('p',) gives a single `p` predicate, and PPR passes ('ps', 'pd') for
    its two edge endpoints. With the flag off the information_schema probe is skipped, so the common
    path costs no query. The fragments are built from a fixed alias set, never from user input, so
    there is no injection surface.
    """
    if get_settings().temporal_current_only_enabled and await _pit_status_col_present(conn):
        return "".join("\n                  " + _CURRENT_ONLY_PRED_TMPL.format(a=a) for a in aliases)
    return ""


def _embedder(name: str):
    """Return `(column, model_loader, vec_formatter)` for an embedder name.

    'bge' is bge-m3 (1024-d); anything else is the local mpnet model (768-d).
    """
    if name == "bge":
        from pythia.ingest.embed_bge import _vec, model

        return "embedding_bge", model, _vec
    from pythia.ingest.embed_local import _vec, model

    return "embedding_local", model, _vec


# ------------------------------------------------------------------------- version-aware as-of text
# The seed and CTE in-force masks read provision.text and the head row's valid_from/valid_to. After
# an amendment the head row's valid_from stays at the provision's origin (the first version's
# effective date) while its text is the new version, so a past as_of would serve the new text
# instead of the old one. The version lineage lives in provision_version, one row per
# [valid_from, valid_to) window. When temporal_substrate_enabled is on and as_of is in the past,
# these helpers overlay each seed with the text of the version whose half-open window contains
# as_of. Otherwise they are skipped and Seed.asof_text stays None.
#
# Scope: the overlay only sets Seed.asof_text. The as-of renderer (engine._decorate, gated by
# temporal_asof_render_enabled) serves it as text_md on the /v1/retrieve path only; it does not
# correct the /v1/answer prose path and does not affect ranking. At a past as_of the candidate set
# and order still come from the current head embedding and tsv index. On a single-version corpus the
# overlay returns the head text anyway (the first version's window is open-ended), so it only makes
# a difference once an amendment adds a second version.
#
# Not built: ranking by the text in force at a past as_of (rather than only serving it) would need a
# tsv and embedding column on provision_version, filled per version, with sparse_seed/dense_seed
# searching the window-selected version row instead of the head. That is a schema change plus a
# bge-m3 re-embedding pass over the whole lineage. These helpers correct the served text only and
# never claim historical semantic recall.
def _past_as_of(as_of: date | None) -> bool:
    """Return whether as_of is strictly before today.

    None, today and future dates use the head row, which is the current version, so the version
    overlay would be a no-op and the extra query is skipped.
    """
    return as_of is not None and as_of < date.today()


async def _asof_version_text(conn, ids: list[str], as_of: date) -> dict[str, str]:
    """Return `{provision_id: text}` for the version of each provision in force at as_of.

    Uses the provision_version lineage keyed by (source_document_id, label) and picks the version
    whose half-open window contains as_of (valid_from <= as_of < valid_to; a NULL valid_to is
    open-ended). Ids without a matching version (no lineage, or as_of before the first version) are
    absent, so the caller keeps the head text rather than inventing a past one. One indexed read.
    """
    if not ids:
        return {}
    rows = await conn.fetch(
        """SELECT p.id::text AS id, pv.text AS text
             FROM provision p
             JOIN LATERAL (
                 SELECT v.text
                   FROM provision_version v
                  WHERE v.source_document_id = p.source_document_id
                    AND v.provision_label = p.label
                    AND (v.valid_from IS NULL OR v.valid_from <= $2::date)
                    AND (v.valid_to   IS NULL OR v.valid_to   >  $2::date)
                  ORDER BY v.version_no DESC
                  LIMIT 1) pv ON true
            WHERE p.id = ANY($1::uuid[])""",
        ids,
        as_of,
    )
    return {r["id"]: r["text"] for r in rows}


async def _overlay_asof_text(conn, seeds: list[Seed], as_of: date | None) -> list[Seed]:
    """Set Seed.asof_text to the version-in-force text when as_of is in the past.

    Only active when temporal_substrate_enabled is on and as_of is strictly before today; otherwise
    `seeds` is returned unchanged with asof_text left None. It only sets asof_text and never
    reorders or drops a seed. The text is served by the as-of renderer in engine._decorate (gated
    separately by temporal_asof_render_enabled); see the scope note above.
    """
    if not seeds or not _past_as_of(as_of) or not get_settings().temporal_substrate_enabled:
        return seeds
    if not await conn.fetchval("SELECT to_regclass('public.provision_version') IS NOT NULL"):
        return seeds  # no lineage table, nothing to overlay
    texts = await _asof_version_text(conn, [s.provision_id for s in seeds], as_of)
    for s in seeds:
        if s.provision_id in texts:
            s.asof_text = texts[s.provision_id]
    return seeds


# --------------------------------------------------------------------------------- clause read path
# An additional dense leg, used only when config.clause_read_enabled is on (env
# PYTHIA_CLAUSE_READ_ENABLED, default off). It runs an ANN search over
# provision_clause.clause_embedding (the same normalised bge-m3 1024-d space) and maps each clause
# hit up to its parent provision_id, so every downstream consumer (graph expansion, GNN, references)
# stays anchored at the § level. Clause granularity improves the seed entry only, never the unit the
# graph walks.
async def _clause_parent_seed(
    qv: str, *, k: int, jurisdiction: list[str] | None, as_of: date | None, include_case_law: bool
) -> list[Seed]:
    """Seed parent provisions from an ANN search over their clauses.

    `qv` is the formatted query vector dense_seed already built for the bge column, so the query is
    not re-encoded. Returns at most `k` Seeds keyed by parent provision_id, each scored by its best
    clause (maximum cosine). The parent's masks (validity window, jurisdiction, case-law exclusion)
    are re-applied through the join, so the clause leg can never surface a provision that the
    §-level dense_seed would exclude. Returns an empty list if the clause table has not been built.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Clause table not built yet: no clause leg, and not an error.
        if not await conn.fetchval("SELECT to_regclass('public.provision_clause') IS NOT NULL"):
            return []
        current_only = await current_only_clause(conn)  # "" unless the current_only filter is active
        rows = await conn.fetch(
            f"""SELECT p.id::text AS id, max(1 - (pc.clause_embedding <=> $1::vector)) AS rank
                 FROM provision_clause pc
                 JOIN provision p ON p.id = pc.provision_id
                WHERE pc.clause_embedding IS NOT NULL
                  AND p.is_anchor_only = false                          -- B1a-03: never map a clause hit up
                                                                        -- to a structural anchor (defensive)
                  AND (p.valid_to IS NULL OR p.valid_to > COALESCE($4::date, CURRENT_DATE))
                  AND (p.valid_from IS NULL OR p.valid_from <= COALESCE($4::date, CURRENT_DATE)){current_only}
                  AND ($3::text[] IS NULL OR p.jurisdiction = ANY($3::text[]))
                  AND ($5::boolean OR p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW')
                GROUP BY p.id
                ORDER BY rank DESC
                LIMIT $2""",
            qv,
            k,
            (jurisdiction or None),
            as_of,
            include_case_law,
        )
    return [Seed(r["id"], float(r["rank"])) for r in rows]


async def sparse_seed(
    query: str,
    *,
    k: int = 8,
    jurisdiction: list[str] | None = None,
    as_of: date | None = None,
    include_case_law: bool = False,
) -> list[Seed]:
    """Seed provisions by full-text search over provision.tsv.

    Terms are OR-combined, because plainto_tsquery ANDs every term and a multi-word query would then
    match no single provision; ts_rank still favours provisions that match more of the query.

    By default (`include_case_law=False`) CASE_LAW provisions are excluded, so a ruling never
    surfaces inside a statute answer. `include_case_law=True` lifts the filter for the explicit
    opt-in path.
    """
    # Token selection: the first 14 tokens by default, or IDF-ranked when seed_token_idf_enabled is
    # on, so a discriminative legal term late in a long question is not dropped. Both bound the
    # query to 14 terms.
    terms = await _seed_terms(query, idf=get_settings().seed_token_idf_enabled)
    if not terms:
        return []
    tsq = " | ".join(terms)
    juris = jurisdiction if jurisdiction else None
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_only = await current_only_clause(conn)  # "" unless the current_only filter is active
        rows = await conn.fetch(
            f"""SELECT p.id::text AS id, ts_rank(p.tsv, to_tsquery('czech', $1)) AS rank
                 FROM provision p
                WHERE p.tsv @@ to_tsquery('czech', $1)
                  AND p.is_anchor_only = false                          -- B1a-03: structural section anchors
                                                                        -- (no text/tsv) NEVER seed retrieval
                  AND (p.valid_to IS NULL OR p.valid_to > COALESCE($4::date, CURRENT_DATE))
                  AND (p.valid_from IS NULL OR p.valid_from <= COALESCE($4::date, CURRENT_DATE))  -- already in force at as_of (point-in-time lower bound){current_only}
                  AND ($3::text[] IS NULL OR p.jurisdiction = ANY($3::text[]))
                  AND ($5::boolean OR p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW')
                ORDER BY rank DESC
                LIMIT $2""",
            tsq,
            k,
            juris,
            as_of,
            include_case_law,
        )
        seeds = [Seed(r["id"], float(r["rank"])) for r in rows]
        return await _overlay_asof_text(conn, seeds, as_of)  # past as_of: version-in-force text


async def dense_seed(
    query: str,
    *,
    k: int = 8,
    jurisdiction: list[str] | None = None,
    as_of: date | None = None,
    embedder: str = DEFAULT_EMBEDDER,
    include_case_law: bool = False,
) -> list[Seed]:
    """Seed provisions by semantic similarity to the query.

    Embeds the query with the chosen local model and runs an ANN search over the matching provision
    embedding column (pgvector cosine), so it matches by meaning and across languages. By default
    (`include_case_law=False`) CASE_LAW provisions are excluded; `include_case_law=True` lifts the
    filter.
    """
    col, model, _vec = _embedder(embedder)
    qv = _vec(model().encode(query, normalize_embeddings=True))
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_only = await current_only_clause(conn)  # "" unless the current_only filter is active
        rows = await conn.fetch(
            f"""SELECT p.id::text AS id, 1 - (p.{col} <=> $1::vector) AS rank
                 FROM provision p
                WHERE p.{col} IS NOT NULL
                  AND p.is_anchor_only = false                          -- B1a-03: structural section anchors
                                                                        -- (no embedding) NEVER seed retrieval
                  AND (p.valid_to IS NULL OR p.valid_to > COALESCE($4::date, CURRENT_DATE))
                  AND (p.valid_from IS NULL OR p.valid_from <= COALESCE($4::date, CURRENT_DATE))  -- already in force at as_of (point-in-time lower bound){current_only}
                  AND ($3::text[] IS NULL OR p.jurisdiction = ANY($3::text[]))
                  AND ($5::boolean OR p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW')
                ORDER BY p.{col} <=> $1::vector
                LIMIT $2""",
            qv,
            k,
            (jurisdiction or None),
            as_of,
            include_case_law,
        )
    base = [Seed(r["id"], float(r["rank"])) for r in rows]
    # Clause read path (clause_read_enabled, default off; bge column only, which the clause vectors
    # share): search the clause index, map hits to their parent provisions and merge them into the
    # §-level set, keeping each provision's best score across both legs, re-sorting and truncating
    # back to k. The seeds remain parent provision ids, so graph expansion stays anchored at the §
    # level.
    if get_settings().clause_read_enabled and col == "embedding_bge":
        clause = await _clause_parent_seed(
            qv, k=k, jurisdiction=jurisdiction, as_of=as_of, include_case_law=include_case_law
        )
        if clause:
            best: dict[str, float] = {}
            for s in base + clause:
                if s.provision_id not in best or s.score > best[s.provision_id]:
                    best[s.provision_id] = s.score
            base = [Seed(i, best[i]) for i in sorted(best, key=lambda i: -best[i])[:k]]
    # Past as_of: overlay version-in-force text on the final set (after clause merging). The ANN
    # query above has already released its connection, so acquire a new one; when the overlay is
    # inactive (flag off or a current as_of) no connection is acquired at all.
    if _past_as_of(as_of) and get_settings().temporal_substrate_enabled:
        async with pool.acquire() as conn:
            base = await _overlay_asof_text(conn, base, as_of)
    return base


async def hybrid_seed(
    query: str,
    *,
    k: int = 8,
    jurisdiction: list[str] | None = None,
    as_of: date | None = None,
    embedder: str = DEFAULT_EMBEDDER,
    rerank: bool = False,
    include_case_law: bool = False,
    rerank_pool: int | None = None,
) -> list[Seed]:
    """Fuse sparse (keyword) and dense (semantic) seeds by reciprocal-rank fusion.

    Optionally reranks the fused shortlist with the cross-encoder. Falls back to sparse alone if no
    embeddings exist. `include_case_law` (default False) is passed to both legs, so by default the
    fused seed set excludes rulings.

    `rerank_pool` overrides the floor of the rerank candidate pool: the pool is
    max(2 * k, rerank_pool), or max(2 * k, _RERANK_POOL) when None. answer() sets it (under
    seed_pool_scale_enabled) to widen the shortlist for multi-aspect questions. It changes only the
    pool size, never the fusion math or the final top-k count.
    """
    rk = max(k * 2, _RERANK_POOL if rerank_pool is None else rerank_pool)  # rerank candidate pool
    pool_k = rk if rerank else k * 2
    # The keyword and semantic legs use independent indexes, so fetch them concurrently.
    sparse, dense = await asyncio.gather(
        sparse_seed(query, k=pool_k, jurisdiction=jurisdiction, as_of=as_of, include_case_law=include_case_law),
        dense_seed(
            query,
            k=pool_k,
            jurisdiction=jurisdiction,
            as_of=as_of,
            embedder=embedder,
            include_case_law=include_case_law,
        ),
        return_exceptions=True,
    )
    if isinstance(sparse, BaseException):
        raise sparse  # the keyword leg is the base and the fallback, so it must succeed
    if isinstance(dense, BaseException):
        dense = []  # the dense leg (embeddings) is optional; degrade gracefully
    # Term normalisation (term_norm_enabled, default off). Resolves the query against the term
    # lexicon (defined terms plus the alias file) and adds two things: (a) an extra sparse leg over
    # the statutory wording of matched terms, so a concept with no lexical overlap (cookies,
    # 'koncové zařízení') still reaches the tsvector index, and (b) the defining provisions as extra
    # seeds. It only adds candidates and never removes or re-orders an existing hit; it never blocks
    # retrieval either.
    term_extra: list[Seed] = []
    term_seed_ids: list[str] = []
    if get_settings().term_norm_enabled:
        from pythia.graph.term_norm import term_norm_augmentation

        boost, term_seed_ids = await term_norm_augmentation(query)
        if boost:
            extra = await sparse_seed(
                " ".join(boost), k=pool_k, jurisdiction=jurisdiction, as_of=as_of, include_case_law=include_case_law
            )
            if isinstance(extra, list):
                term_extra = extra
    if not dense and not rerank and not term_extra and not term_seed_ids:
        return await _hybrid_overlay(sparse[:k], as_of)
    rrf: dict[str, float] = {}
    for lst in (sparse, dense, term_extra):
        for rank, s in enumerate(lst):
            rrf[s.provision_id] = rrf.get(s.provision_id, 0.0) + 1.0 / (60 + rank)
    # Defining-provision seeds from term normalisation: guarantee the defining § is in the candidate
    # pool. Each gets a floor RRF score strictly below any real leg hit (1 / (60 + pool_k + 1)) so
    # it never outranks a genuine match; a provision that already scored from a leg keeps its higher
    # score.
    _floor = 1.0 / (60 + pool_k + 1)
    for pid in term_seed_ids:
        if pid not in rrf:
            rrf[pid] = _floor
    cand = sorted(rrf, key=lambda i: -rrf[i])[: (rk if rerank else k)]
    if rerank and cand:
        from pythia.graph.rerank import rerank as _rerank

        dbpool = await get_pool()
        async with dbpool.acquire() as conn:
            texts = {
                str(r["id"]): r["text"]
                for r in await conn.fetch(
                    "SELECT id::text id, left(text, 2000) text FROM provision WHERE id = ANY($1::uuid[])", cand
                )
            }
        ranked = _rerank(query, [(i, texts[i]) for i in cand if i in texts], top_k=k)
        return await _hybrid_overlay([Seed(i, round(s, 5)) for i, s in ranked], as_of)
    return await _hybrid_overlay([Seed(i, round(rrf[i], 5)) for i in cand[:k]], as_of)


async def _hybrid_overlay(seeds: list[Seed], as_of: date | None) -> list[Seed]:
    """Apply the past-as_of version-text overlay to a hybrid result set.

    hybrid_seed rebuilds Seed objects from the RRF scores (and the rerank path reads the head text
    for the cross-encoder), which drops the asof_text the individual legs carried, so it is
    re-derived on the final set. No connection is acquired when the overlay is inactive. The
    cross-encoder still ranks on head text; only the served text is version-corrected (see the scope
    note above).
    """
    if not seeds or not _past_as_of(as_of) or not get_settings().temporal_substrate_enabled:
        return seeds
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await _overlay_asof_text(conn, seeds, as_of)


async def _demo(query: str) -> None:
    seeds = await sparse_seed(query, k=8)
    pool = await get_pool()
    async with pool.acquire() as conn:
        labels = {
            str(r["id"]): dict(r)
            for r in await conn.fetch(
                """SELECT p.id, p.label, p.jurisdiction, sd.title FROM provision p
                 JOIN source_document sd ON sd.id = p.source_document_id
                WHERE p.id = ANY($1::uuid[])""",
                [s.provision_id for s in seeds],
            )
        }
    print(f"QUERY: {query}\nseeds:")
    for s in seeds:
        m = labels.get(s.provision_id, {})
        print(
            f"  r={s.score:.4f}  {(m.get('label') or '?'):<14} [{m.get('jurisdiction')}] {(m.get('title') or '')[:48]}"
        )
    await close_pool()


if __name__ == "__main__":
    asyncio.run(_demo(sys.argv[1] if len(sys.argv) > 1 else "nabízení investičního fondu marketing notifikace"))
