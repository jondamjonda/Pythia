"""NexusPort: the single door to Nexus, the legal knowledge graph.

Every consumer of the knowledge base (the engine's own reasoning layers, and the app over HTTP via
the sidecar) goes through `NexusPort` and nothing else. The graph and GNN packages are the
implementation behind this port, and only this module imports them; tests/test_nexus_port_boundary.py
fails if a consumer module imports them directly or queries the graph tables with raw SQL.

NexusPort offers query-agnostic knowledge access; query-dependent orchestration lives in `engine.py`
and `reasoning/`:
  - search(query): entry retrieval (hybrid lexical and dense seeds)
  - candidates(...): metadata-filtered candidates (jurisdiction, domain, hierarchy)
  - expand(query, seeds): multi-hop graph expansion (cte, ppr or gnn) with degraded/warnings metadata
  - fetch_provisions(ids): provision metadata and trust facets, the one shape used everywhere
  - rulings_for(provision): the opt-in case-law panel
  - browse() / act_tree(ext): faceted navigation and an act's section tree
  - importance(ids): structural centrality and regime community
The SQL read paths (search, candidates, expand with cte or ppr, fetch, rulings, act_tree) take `as_of`
(point-in-time law); the [valid_from, valid_to) mask lives in the implementation modules. The one
exception is expand(method="gnn"): its in-memory graph walk does not apply `as_of`, only the
fail-open `_current_only_hits` post-filter (which drops hits known not to be current when
temporal_current_only_enabled is on). A past `as_of` is therefore not honoured on the gnn path, which
is not the default (the app and sidecar default to "cte").

The Embed, LLM and Telemetry protocols at the bottom declare the remaining host-agnostic seams.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from pythia.config import get_settings
from pythia.db import get_pool

# Nexus implementation modules, imported only here (the single door).
from pythia.graph.browse import act_tree as _act_tree
from pythia.graph.browse import browse_index as _browse_index
from pythia.graph.cte_expand import NeighborHit, RulingHit, four_corners, rulings_for_provision
from pythia.graph.fusion import FusionParams, fuse, is_protected
from pythia.graph.nexus_stats import nexus_events as _nexus_events
from pythia.graph.nexus_stats import nexus_health as _nexus_health
from pythia.graph.nexus_stats import nexus_stats as _nexus_stats
from pythia.graph.ppr import ppr_retrieve
from pythia.graph.role_classifier import ROLES
from pythia.graph.seed import Seed, _CURRENT_ONLY_PRED, _asof_version_text, _past_as_of, hybrid_seed

# The pure trust resolver and its tier vocabulary, re-exported so consumers (engine._decorate) can
# resolve a per-provision TrustTag without importing the graph implementation. resolve_trust reads
# only columns already on ProvisionRow, so it adds no database round trip.
from pythia.graph.trust_resolve import TIER_ORDER, resolve_trust

# Re-exported so consumers import Nexus types and vocabulary from the port, never the implementation.
__all__ = [
    "NexusPort",
    "ProvisionRow",
    "ExpandResult",
    "ClauseRow",
    "NeighborHit",
    "RulingHit",
    "Seed",
    "ROLES",
    "resolve_trust",
    "TIER_ORDER",
    "FusionParams",
    "fuse",
    "is_protected",
]


@dataclass
class ProvisionRow:
    """A provision's metadata and trust facets: the one shape every consumer reads."""

    id: str
    label: str | None
    text: str | None
    jurisdiction: str | None
    legal_hierarchy: str | None
    valid_from: date | None
    valid_to: date | None
    source_title: str | None
    external_id: str | None
    bindingness: str | None
    is_authoritative: bool | None
    temporal_status: str | None
    analysis_roles: list[int] | None  # role-tag codes (labels in ROLES) for packet assembly
    # Inputs to section-level trust resolution, read by resolve_trust. All default to None so callers
    # that do not select them (such as candidates()) still build a ProvisionRow.
    parent_id: str | None = None  # the parent in the hierarchy (target of the anchor walk-up)
    is_anchor_only: bool | None = None  # a structural anchor (never a leaf rule itself)
    section_kind: str | None = None  # the provision's own section_kind (NULL on leaves; see inherited_kind)
    kind_override: str | None = None  # per-provision human escape hatch (resolver prefers it)
    inherited_kind: str | None = None  # the nearest non-null section_kind walked UP the parent_id chain
    annulment_effect: str | None = None  # source_document.annulment_effect (EX_NUNC / EX_TUNC) for court annulment
    annuls_target: bool | None = None  # is this provision the target of an annuls edge (rel 27 / 28)?
    # Per-provision temporal facets. Both default to None, and _key tolerates their absence, so a
    # corpus without the substrate columns still builds a ProvisionRow. pit_status is the provision's
    # own point-in-time status, distinct from the document-level temporal_status. _provision_cols
    # selects them only when a temporal consumer flag is on and the columns exist. They are display
    # facets for the temporal consumer flag; pit_status also feeds the optional down-weighting and
    # prose-guard paths, each behind its own flag.
    pit_status: str | None = None  # IN_FORCE / SUPERSEDED / NOT_YET_IN_FORCE (provision.pit_status)
    stale_vector: bool | None = None  # an embedding exists and embedded_text_hash <> text_hash


@dataclass
class ExpandResult:
    """Graph-expansion hits plus the GNN fail-soft signal (degraded and warnings are empty for cte/ppr)."""

    hits: list[NeighborHit]
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ClauseRow:
    """One row of the `provision_clause` index, as returned by NexusPort.clauses_for.

    Mirrors the columns clause_index.ensure_clause_table writes: the stable within-provision key,
    the human clause label ("(2)" / "a)"), the embedded clause_text ("<heading>\\n<segment>") and
    the 0-based ordinal. Clause snapping reads `clause_label` and `clause_text` (with the heading
    stripped) to locate the clause's span inside the provision's full text.
    """

    provision_id: str
    clause_path: str
    clause_label: str | None
    clause_text: str
    ordinal: int | None


_PROVISION_COLS = """p.id::text AS id, p.label, p.text, p.jurisdiction, p.legal_hierarchy,
       p.valid_from, p.valid_to, sd.title AS source_title, sd.external_id,
       sd.bindingness, sd.is_authoritative, sd.temporal_status, p.analysis_roles,
       p.parent_id::text AS parent_id, p.is_anchor_only, p.section_kind, p.kind_override,
       sd.annulment_effect"""

# Extra columns appended to _PROVISION_COLS only when a temporal consumer flag is on and the substrate
# columns exist: the per-provision status (p.pit_status) and a computed stale-vector flag (a vector
# exists that was embedded from older text), aliased to the two ProvisionRow facets _row() reads via
# _key(). They never enter a WHERE or ORDER BY, so they cannot change which provisions are surfaced
# or their order. `IS DISTINCT FROM` makes the stale flag NULL-safe (a NULL embedded_text_hash on an
# embedded row counts as stale). The expression matches the stale_vector_ids() predicate
# (`embedding_bge IS NOT NULL AND NOT is_anchor_only AND embedded_text_hash IS DISTINCT FROM
# text_hash`), so an anchor-only row, which has no real vector, is never reported stale.
_PROVISION_COLS_PIT_TAIL = """,
       p.pit_status AS pit_status,
       (p.embedding_bge IS NOT NULL AND p.is_anchor_only IS NOT TRUE
        AND p.embedded_text_hash IS DISTINCT FROM p.text_hash) AS stale_vector"""

# The substrate columns the tail references. If any is missing (a corpus that never activated the
# temporal substrate), selecting them would fail, so the plain select is used instead (None facets).
_PIT_REQUIRED_COLS: tuple[str, ...] = (
    "pit_status",
    "embedded_text_hash",
    "text_hash",
    "embedding_bge",
    "is_anchor_only",
)


async def _pit_cols_present(conn) -> bool:
    """Return True if every column the temporal tail references exists on `provision`.

    When one is missing the caller uses the plain select.
    """
    n = await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name='provision' AND column_name = ANY($1::text[])",
        list(_PIT_REQUIRED_COLS),
    )
    return int(n or 0) == len(_PIT_REQUIRED_COLS)


async def _pit_status_col_present(conn) -> bool:
    """Return True if provision.pit_status exists.

    It is the only column the current-only filter needs, so this is a narrower check than
    _pit_cols_present (which also requires the stale-vector columns). When it is missing the caller
    adds no pit_status predicate.
    """
    return bool(
        await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name='provision' AND column_name='pit_status')"
        )
    )


async def _current_only_filter(conn) -> str:
    """Return the current-only predicate as a SQL fragment for candidates()' WHERE clause.

    Returns "" unless config.temporal_current_only_enabled is on and provision.pit_status exists.
    Otherwise it is a fail-open per-provision filter: keep IN_FORCE and NULL (no version lineage
    means unknown, which is never silently dropped) and exclude only SUPERSEDED and
    NOT_YET_IN_FORCE. It deliberately avoids the partial ix_provision_in_force index, which would
    also drop current law without lineage; the document-level valid_from / valid_to mask remains the
    primary in-force gate. The fragment is a constant, so there is no injection surface. Its text is
    seed._CURRENT_ONLY_PRED, shared with the seed and CTE filters.
    """
    if get_settings().temporal_current_only_enabled and await _pit_status_col_present(conn):
        return "\n                       " + _CURRENT_ONLY_PRED
    return ""


async def _provision_cols(conn) -> str:
    """Return the SELECT column list for provision rows.

    The pit_status / stale_vector tail is appended only when temporal_consumer_enabled,
    temporal_downweight_enabled or temporal_prose_guard_enabled is on and the substrate columns
    exist. Down-weighting needs pit_status so resolve_trust(downweight=True) can refine
    temporal_status, and the prose guard needs it so reasoning/answer.py can derive the caveat, even
    with the display flag off. Otherwise this is exactly _PROVISION_COLS and _row() leaves both
    facets None.
    """
    s = get_settings()
    if (
        s.temporal_consumer_enabled or s.temporal_downweight_enabled or s.temporal_prose_guard_enabled
    ) and await _pit_cols_present(conn):
        return _PROVISION_COLS + _PROVISION_COLS_PIT_TAIL
    return _PROVISION_COLS


def _key(r, key, default=None):
    """Return column `key` of an asyncpg Record if present, else `default`.

    Records have no .get(), but support `key in r`. This lets rows that omit optional columns (for
    example candidates() rows without the walk-up columns) still build a ProvisionRow.
    """
    try:
        return r[key] if key in r else default
    except (KeyError, TypeError):
        return default


def _row(r) -> ProvisionRow:
    return ProvisionRow(
        id=str(r["id"]),
        label=r["label"],
        text=r["text"],
        jurisdiction=r["jurisdiction"],
        legal_hierarchy=r["legal_hierarchy"],
        valid_from=r["valid_from"],
        valid_to=r["valid_to"],
        source_title=r["source_title"],
        external_id=r["external_id"],
        bindingness=r["bindingness"],
        is_authoritative=r["is_authoritative"],
        temporal_status=r["temporal_status"],
        analysis_roles=r["analysis_roles"],
        parent_id=_key(r, "parent_id"),
        is_anchor_only=_key(r, "is_anchor_only"),
        section_kind=_key(r, "section_kind"),
        kind_override=_key(r, "kind_override"),
        inherited_kind=_key(r, "inherited_kind"),
        annulment_effect=_key(r, "annulment_effect"),
        annuls_target=_key(r, "annuls_target"),
        pit_status=_key(r, "pit_status"),
        stale_vector=_key(r, "stale_vector"),
    )


class NexusPort:
    """The single interface to Nexus.

    Hold one per process (it lazily caches the GNN retriever). All methods are async and delegate to
    the implementation modules behind this door.
    """

    def __init__(self) -> None:
        self._gnn = None  # lazily built GNNRetriever (keeps torch out of the import path)

    # Retrieval primitives
    async def search(
        self,
        query: str,
        *,
        k: int = 8,
        jurisdiction: list[str] | None = None,
        as_of: date | None = None,
        rerank: bool = True,
        rerank_pool: int | None = None,
    ) -> list[Seed]:
        """Return seed provisions for a query (hybrid lexical and dense, with optional rerank).

        `rerank_pool` optionally widens the rerank candidate pool; None lets hybrid_seed use its
        _RERANK_POOL floor.
        """
        return await hybrid_seed(
            query, k=k, jurisdiction=jurisdiction, as_of=as_of, rerank=rerank, rerank_pool=rerank_pool
        )

    async def expand(
        self,
        query: str,
        seed_ids: list[str],
        *,
        method: str = "cte",
        k: int = 20,
        max_hops: int = 3,
        as_of: date | None = None,
        include_case_law: bool = False,
    ) -> ExpandResult:
        """Expand from the seeds over multiple hops; `method` is "cte" (four corners), "ppr" or "gnn".

        The gnn path is fail-soft: an incompatible or empty checkpoint yields finite scores flagged
        via ExpandResult.degraded / .warnings instead of raising. cte and ppr are never degraded.
        """
        if method == "ppr":
            hits = await ppr_retrieve(seed_ids, k=k, as_of=as_of, include_case_law=include_case_law)
            return ExpandResult(hits)
        if method == "gnn":
            # The gnn walk runs in memory and does not apply `as_of`, so a past as_of is not honoured
            # here. The flag-gated `_current_only_hits` post-filter is the only temporal mask on this
            # path (fail-open: it drops only hits known not to be current). Honouring a past as_of
            # would need per-date GNN caching, which does not exist yet; gnn is not the default.
            gr = await self._gnn_retriever()
            hits = gr.retrieve(query, seed_ids, k=k)
            hits = await self._current_only_hits(hits)  # drop known-not-current hits (flag-gated)
            return ExpandResult(hits, degraded=bool(gr.untrained), warnings=list(gr.load_warnings))
        hits = await four_corners(seed_ids, max_hops=max_hops, k=k, as_of=as_of, include_case_law=include_case_law)
        return ExpandResult(hits)

    async def _gnn_retriever(self):
        if self._gnn is None:
            from pythia.gnn.retriever import GNNRetriever  # lazy: torch only when method='gnn' is used

            tr = pathlib.Path(__file__).resolve().parent.parent / "train"
            ckpt = next((p for p in (tr / "gnn_stage2.pt", tr / "gnn_stage1.pt") if p.exists()), None)
            self._gnn = await GNNRetriever.load(weights_path=str(ckpt) if ckpt else None)
        return self._gnn

    async def _current_only_hits(self, hits: list[NeighborHit]) -> list[NeighborHit]:
        """Drop GNN hits known not to be current when temporal_current_only_enabled is on.

        The in-memory GNN walk has no SQL mask, so its hits are post-filtered: pit_status SUPERSEDED or
        NOT_YET_IN_FORCE is dropped, while IN_FORCE, NULL and unknown ids are kept (fail-open, the same
        semantics as the SQL predicate on the other paths). Returns `hits` unchanged when the flag is
        off or the pit_status column does not exist. Because the filter runs after the walk rather
        than before a LIMIT, the gnn path can return fewer than k hits.
        """
        if not hits or not get_settings().temporal_current_only_enabled:
            return hits
        ids = [h.provision_id for h in hits]
        pool = await get_pool()
        async with pool.acquire() as conn:
            if not await _pit_status_col_present(conn):  # no pit_status column: nothing to filter on
                return hits
            stale = {
                r["id"]
                for r in await conn.fetch(
                    "SELECT id::text AS id FROM provision "
                    "WHERE id = ANY($1::uuid[]) AND pit_status IN ('SUPERSEDED', 'NOT_YET_IN_FORCE')",
                    ids,
                )
            }
        return [h for h in hits if h.provision_id not in stale] if stale else hits

    # Semantic scoring for the fusion re-rank (read-only)
    async def score_semantic(self, query: str, ids: list[str]) -> dict[str, float]:
        """Return the exact bge-m3 cosine between the query and each given provision.

        Embeds the query once with the same encoder seed.dense_seed uses, then runs an exact-distance
        (not ANN) query over provision.embedding_bge for exactly these ids. Returns
        {id: cosine in [-1, 1]} only for ids that have an embedding; the caller treats the others as
        graph-only. (bge-m3 vectors are normalized and legal-text similarities are in practice
        non-negative, but the range is [-1, 1], not [0, 1].) Empty ids give {}.
        """
        if not ids:
            return {}
        from pythia.ingest.embed_bge import _vec, model

        qv = _vec(model().encode(query, normalize_embeddings=True))
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT p.id::text AS id, 1 - (p.embedding_bge <=> $1::vector) AS cos
                     FROM provision p
                    WHERE p.id = ANY($2::uuid[]) AND p.embedding_bge IS NOT NULL""",
                qv,
                ids,
            )
        return {r["id"]: float(r["cos"]) for r in rows}

    async def score_cross(
        self, query: str, candidates: list[tuple[str, str]], *, top_k: int | None = None
    ) -> list[tuple[str, float]]:
        """Rerank an (id, text) shortlist with the bge-reranker-v2-m3 cross-encoder (advisory).

        The model runs synchronously. The deterministic cosine score (score_semantic) is the
        load-bearing signal; this is an optional precision booster. Empty input gives [].

        Not called anywhere at the moment: `fuse()` uses only the cosine score
        (FusionParams.ce_enabled defaults to False). It is kept as the seam for that rerank path, and
        torch and the reranker are loaded only when it is called.
        """
        if not candidates:
            return []
        from pythia.graph.rerank import rerank

        return rerank(query, candidates, top_k=top_k)

    # Point-in-time version text, for the as-of renderers
    async def asof_text(self, ids: list[str], as_of: date | None) -> dict[str, str]:
        """Return the version text in force at a past `as_of` for each given provision.

        Returns ``{provision_id: version text}`` for ids whose provision_version half-open
        ``[valid_from, valid_to)`` window contains ``as_of`` (the same lookup the seed and CTE paths
        use). Returns ``{}`` unless ``as_of`` is strictly in the past, ``temporal_substrate_enabled`` is
        on and the version table exists; the caller then keeps the head text. Read-only. The as-of
        renderer in reasoning/answer.py uses this instead of importing the graph implementation.
        """
        if not ids or not _past_as_of(as_of) or not get_settings().temporal_substrate_enabled:
            return {}
        pool = await get_pool()
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('public.provision_version') IS NOT NULL"):
                return {}
            return await _asof_version_text(conn, ids, as_of)

    # Clause index, for clause-snapped context windows
    async def clauses_for(self, ids: list[str]) -> dict[str, list[ClauseRow]]:
        """Return the `provision_clause` rows for the given provision ids, ordered by ordinal.

        Returns ``{provision_id: [ClauseRow, ...]}``; a provision without clause rows is absent from
        the map. If the `provision_clause` table does not exist (the clause index was never built) or
        has no rows for these ids, returns ``{}`` and never raises, so clause snapping falls back to
        the unsnapped window. Read-only: it only selects from the existing index. Empty ids give ``{}``.
        """
        if not ids:
            return {}
        pool = await get_pool()
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT to_regclass('public.provision_clause') IS NOT NULL"):
                return {}  # index never built: no snapping
            rows = await conn.fetch(
                """SELECT provision_id::text AS pid, clause_path, clause_label, clause_text, ordinal
                     FROM provision_clause
                    WHERE provision_id = ANY($1::uuid[])
                    ORDER BY provision_id, ordinal""",
                ids,
            )
        out: dict[str, list[ClauseRow]] = {}
        for r in rows:
            out.setdefault(r["pid"], []).append(
                ClauseRow(
                    provision_id=r["pid"],
                    clause_path=r["clause_path"],
                    clause_label=r["clause_label"],
                    clause_text=r["clause_text"],
                    ordinal=r["ordinal"],
                )
            )
        return out

    # Metadata and candidate generation
    async def fetch_provisions(self, ids: list[str]) -> list[ProvisionRow]:
        """Return metadata and trust facets for the given ids, in input order.

        Also resolves the two trust inputs that are not plain columns. `inherited_kind` is the nearest
        non-null `section_kind` up the parent_id chain (at most 8 levels), computed by a bounded
        WITH RECURSIVE walk per id: section_kind lives on the structural anchors, so a leaf provision
        inherits the kind of the section it sits in (a leaf under a `definitions` group inherits
        `definitions`). `annuls_target` says whether the provision is the target of an annuls edge
        (rel 27 / 28).
        """
        if not ids:
            return []
        pool = await get_pool()
        async with pool.acquire() as conn:
            cols = await _provision_cols(conn)  # adds pit_status / stale_vector when a temporal flag is on
            rows = await conn.fetch(
                f"""WITH RECURSIVE walk(start_id, node_id, kind, depth) AS (
                        -- seed: each requested provision is its own walk root (depth 0).
                        SELECT p0.id, p0.id, p0.section_kind, 0
                          FROM provision p0
                         WHERE p0.id = ANY($1::uuid[])
                      UNION ALL
                        -- climb to the parent ONLY while no classified ancestor has been found yet, bounded
                        -- to ≤8 hops (cast/hlava/dil/oddil/par chains are shallow; the bound caps a cycle).
                        SELECT w.start_id, par.id, par.section_kind, w.depth + 1
                          FROM walk w
                          JOIN provision cur ON cur.id = w.node_id
                          JOIN provision par ON par.id = cur.parent_id
                         WHERE w.kind IS NULL AND w.depth < 8
                    ),
                    inherited AS (
                        -- the nearest classified ancestor's kind per requested id (lowest depth that has one).
                        SELECT DISTINCT ON (start_id) start_id, kind AS inherited_kind
                          FROM walk WHERE kind IS NOT NULL ORDER BY start_id, depth ASC
                    )
                    SELECT {cols},
                           inh.inherited_kind AS inherited_kind,
                           EXISTS (
                               -- annuls-target: rel-27 (annuls: ruling→statute, p is dst) OR
                               --                rel-28 (annulled_by: statute→ruling, p is src).
                               SELECT 1 FROM pythia_edge e
                                WHERE (e.rel_id = 27 AND e.dst = p.id)
                                   OR (e.rel_id = 28 AND e.src = p.id)
                           ) AS annuls_target
                      FROM provision p
                      JOIN source_document sd ON sd.id = p.source_document_id
                      LEFT JOIN inherited inh ON inh.start_id = p.id
                     WHERE p.id = ANY($1::uuid[])""",
                ids,
            )
        by_id = {str(r["id"]): _row(r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def resolve_citation(self, ext: str, sec: str) -> str | None:
        """Resolve an (act external_id, §/Article number) citation to a provision id in the corpus.

        Returns None if the corpus has no such provision. Read-only, at section granularity, and used
        only for verification. This checks existence in the corpus, not in the retrieved set:
        verify.ground() marks a cited section ungrounded when it was not retrieved, and this asks
        whether it exists at all. If it resolves, the provision exists but was not retrieved (a
        retrieval gap); if not, the citation is genuinely not in the corpus or fabricated.

        The section grammar is shared with grounding: both `sec` and each candidate's `label` are
        reduced with verify._sec_num (the same `_SEC_RE` parse verify.ground() uses), so "§ 297(1)",
        "čl. 297" and "297" collapse to the same key on both sides, and the resolver can never disagree
        with grounding about what "§ 297" means. Returns the first matching provision id (any one
        proves existence), or None for an empty ext/sec, an unknown act or no section match.
        """
        from pythia.reasoning.verify import _sec_num  # the shared section grammar (no second regex)

        # `sec` may carry a marker ("§ 318(1)", "čl. 27") or be a bare number (verify.ground() emits the
        # ungrounded section already stripped to "318"). A bare number has no _SEC_RE marker, so fall
        # back to parsing "§ {sec}" with the same grammar. Corpus labels and the target then reduce to
        # the same section key.
        want = _sec_num(sec) or _sec_num(f"§ {sec}")
        if not (ext or "").strip() or not want:
            return None
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT p.id::text AS id, p.label
                     FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                    WHERE sd.external_id = $1 AND p.label IS NOT NULL""",
                ext.strip(),
            )
        for r in rows:
            if _sec_num(r["label"]) == want:  # section match under the shared grammar
                return str(r["id"])
        return None

    async def candidates(
        self,
        *,
        jurisdiction: list[str] | None = None,
        legal_domain: list[str] | None = None,
        legal_hierarchy: list[str] | None = None,
        as_of: date | None = None,
        limit: int = 200,
        include_case_law: bool = False,
    ) -> list[ProvisionRow]:
        """Return in-force law matching the given facets, ranked by structural centrality.

        Pure metadata filtering, with no query or embeddings; it replaces the app's tag-filter step.
        Honours the point-in-time [valid_from, valid_to) mask.

        Statutes only by default (`include_case_law=False`, as in the seed, CTE and PPR paths): a
        jurisdiction-only call must not surface CASE_LAW rulings as candidate law. Case law belongs to
        the opt-in rulings_for panel, never to a silent list of obligations. Set include_case_law=True
        only for an explicitly case-law-inclusive pool.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            cols = await _provision_cols(conn)  # adds pit_status / stale_vector when a temporal flag is on
            current_filter = await _current_only_filter(conn)  # "" unless the current-only flag is on
            rows = await conn.fetch(
                f"""SELECT {cols}, p.centrality
                      FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                     WHERE ($1::text[] IS NULL OR p.jurisdiction = ANY($1::text[]))
                       AND ($2::text[] IS NULL OR sd.legal_domain && $2::text[])
                       AND ($3::text[] IS NULL OR p.legal_hierarchy = ANY($3::text[]))
                       AND ($6::boolean OR p.legal_hierarchy IS DISTINCT FROM 'CASE_LAW')
                       AND (p.valid_to   IS NULL OR p.valid_to   > COALESCE($4::date, CURRENT_DATE))
                       AND (p.valid_from IS NULL OR p.valid_from <= COALESCE($4::date, CURRENT_DATE)){current_filter}
                     ORDER BY p.centrality DESC NULLS LAST
                     LIMIT $5""",
                jurisdiction,
                legal_domain,
                legal_hierarchy,
                as_of,
                limit,
                include_case_law,
            )
        return [_row(r) for r in rows]

    # Case law, navigation and structural intelligence
    async def rulings_for(self, provision_id: str, *, as_of: date | None = None) -> list[RulingHit]:
        """Return the rulings that engage a statute provision, for the opt-in case-law panel.

        Rulings are shown alongside a statute answer, never folded into it, with court, case number,
        title and date for display. `as_of` applies the point-in-time mask (default today).
        """
        return await rulings_for_provision(provision_id, as_of=as_of)

    async def browse(self) -> dict:
        """Return the faceted browse index (facets, regimes, spine, catalogue) for the regulation explorer."""
        return await _browse_index()

    async def act_tree(self, external_id: str, *, as_of: date | None = None) -> dict:
        """Return one act's provision tree, nested by the hierarchy_path ltree and annotated with centrality.

        `as_of` filters to provisions in force at that date; None shows the whole act.
        """
        return await _act_tree(external_id, as_of=as_of)

    async def importance(self, ids: list[str]) -> dict[str, dict]:
        """Return {id: {centrality, community}} for the given provisions.

        A ranking prior: a "these matter most" signal. Ids without scores (isolated provisions) are
        omitted.
        """
        if not ids:
            return {}
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id::text AS id, centrality, community FROM provision WHERE id = ANY($1::uuid[])", ids
            )
        return {r["id"]: {"centrality": r["centrality"], "community": r["community"]} for r in rows}

    # Control-room feeds behind the sidecar's /v1/nexus/* endpoints. The control room is a Nexus
    # consumer too, so its feeds also go through the port.
    async def nexus_stats(self, *, force: bool = False) -> dict:
        """Return corpus, graph, centrality, embedding-coverage, GNN and database-size statistics.

        Includes the graph_freshness headline metric. Cached for about 45 seconds.
        """
        return await _nexus_stats(force=force)

    async def nexus_events(self, limit: int = 50) -> list[dict]:
        """Return recent nexus_event rows (ingest, reindex, materialize, check): the ingestion-runs feed."""
        return await _nexus_events(limit)

    async def nexus_health(self) -> dict:
        """Return a quick Nexus liveness and freshness status (for the sidebar status indicator)."""
        return await _nexus_health()


# Host-agnostic protocols for embedding, LLM and telemetry back ends.
@runtime_checkable
class EmbedPort(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...  # seed embeddings


@runtime_checkable
class LLMPort(Protocol):
    # `stage` is an optional cost-observability label: the paid adapters tag recorded token usage
    # with it, and Ollama ignores it.
    async def json(
        self, prompt: str, schema: dict, *, tier: str, stage: str | None = None
    ) -> dict: ...  # generation and verification


@runtime_checkable
class TelemetryPort(Protocol):
    async def record(
        self, *, source: str, model: str, tokens: dict, cost: float, latency_ms: int
    ) -> None: ...  # admin dashboard
