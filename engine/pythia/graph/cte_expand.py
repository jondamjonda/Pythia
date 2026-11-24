"""Four-corners graph expansion as a recursive CTE over `pythia_edge`.

Training-free and LLM-free. This is the permanent baseline that the GNN must beat. From the seed
provisions it walks typed citation, definition and hierarchy edges up to `max_hops`, keeps only law
in force at the requested date, and returns each reached provision with the relation path that
reached it: the "look behind the four corners of the text" step a lawyer would take.

Run:  .venv/bin/python -m pythia.graph.cte_expand [seed_provision_uuid]
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import date

from pythia.config import get_settings
from pythia.db import close_pool, get_pool
from pythia.graph.relations import NON_TRAVERSABLE_REL_IDS, PANEL_INVERSE_CASE_LAW_REL_IDS

# Relations the walk never traverses (mirrors the relations.py registry). annuls/annulled_by
# (27/28) record erga omnes derogation lineage: a status fact with weight 0, never a navigation
# edge. They are excluded from both the seed step and the recursive step, so they can never become
# a four-corners neighbour, whatever weight they might be given later.
_NON_TRAVERSABLE = "(" + ",".join(str(i) for i in NON_TRAVERSABLE_REL_IDS) + ")"

# Parameters: $1 seed_ids uuid[], $2 max_hops, $3 decay, $4 as_of date or NULL, $5 k,
# $6 include_case_law bool, $7 fanout_cap int (max out-edges expanded per node per hop).
#
# Case-law lane: the walk joins `provision` on every reached node and by default ($6 = false)
# keeps only non-case-law destinations. A statute walk therefore never lands on a CASE_LAW ruling
# node (for example via applied_by(24), statute to ruling), so rulings cannot leak into a statute
# answer. $6 = true lifts this for the explicit opt-in path. The filter applies only to the
# expansion; the seeds ($1) are whatever the caller chose.
#
# Fan-out cap ($7): a recursive CTE costs roughly branching_factor ** max_hops. A definition hub
# (for example a "§ 2" with about 380 out-edges) makes the walk explode: at the default max_hops=3
# a hub-seeded query already exceeds the 250 ms budget, and max_hops=4 takes several seconds. Each
# step therefore expands only a node's top $7 traversable out-edges by weight (a LATERAL ... ORDER
# BY weight DESC LIMIT), with ties broken by dst so the cut is deterministic.
#
# This is a latency heuristic, not a recall-preserving transform. Capping by weight keeps each
# node's strongest edges, which dominate the path score (the product of weight times decay per
# hop), so the top k by score survive in the common case. It is not guaranteed: a result reachable
# only through a low-weight bridge edge into a high-value subtree can be dropped (a real but rare
# recall loss at max_hops >= 2). On the current corpus the only observed differences from an
# uncapped walk are among tied scores at the cut-off, where the weight-then-dst order is in fact
# more deterministic than the uncapped walk; no higher-scoring node is lost.
#
# The seed step uses GREATEST($7, $5) instead of $7: a depth-1 node's score is its seed-edge
# weight, so the true top k depth-1 nodes are the k heaviest seed edges, and the seed step must
# never cap below k. The recursive step uses plain $7.
_FOUR_CORNERS_SQL = f"""
WITH RECURSIVE walk(root, node, depth, path_types, path_nodes, score) AS (
    SELECT s.seed, e.dst, 1,
           ARRAY[e.edge_type]::text[],
           ARRAY[s.seed, e.dst]::uuid[],
           e.weight::double precision          -- weight already encodes w₀·c (v2 §3.4); no 2nd confidence factor
    FROM unnest($1::uuid[]) AS s(seed)
    CROSS JOIN LATERAL (
        SELECT e.dst, e.edge_type, e.weight
        FROM pythia_edge e
        JOIN provision pd ON pd.id = e.dst                 -- gate the reached node's hierarchy
        WHERE e.src = s.seed AND e.dst IS NOT NULL
          AND e.rel_id NOT IN {_NON_TRAVERSABLE}           -- never navigate annuls/annulled_by lineage
          AND ($6::boolean OR pd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW')  -- statute lane: no rulings
        ORDER BY e.weight DESC, e.dst                       -- keep this seed's strongest out-edges …
        LIMIT GREATEST($7::int, $5::int)                    -- … but NEVER below k: a depth-1 node's score
                                                            --     IS its seed-edge weight, so the true
                                                            --     top-k depth-1 nodes are the k highest-
                                                            --     weight seed edges — they must survive.
    ) e
  UNION ALL
    SELECT w.root, e.dst, w.depth + 1,
           w.path_types || e.edge_type,
           w.path_nodes || e.dst,
           w.score * e.weight * $3
    FROM walk w
    CROSS JOIN LATERAL (
        SELECT e.dst, e.edge_type, e.weight
        FROM pythia_edge e
        JOIN provision pd ON pd.id = e.dst                 -- gate the reached node's hierarchy
        WHERE e.src = w.node AND e.dst IS NOT NULL
          AND e.rel_id NOT IN {_NON_TRAVERSABLE}           -- never navigate annuls/annulled_by lineage
          AND ($6::boolean OR pd.legal_hierarchy IS DISTINCT FROM 'CASE_LAW')  -- statute lane: no rulings
          AND NOT (e.dst = ANY(w.path_nodes))             -- cycle guard (inside the cap so it counts only valid next-hops)
        ORDER BY e.weight DESC, e.dst                       -- keep this node's strongest out-edges …
        LIMIT $7                                            -- … capped at the fan-out budget
    ) e
    WHERE w.depth < $2
)
SELECT * FROM (
    SELECT DISTINCT ON (w.node)
           w.node       AS provision_id,
           w.depth      AS hops,
           w.score      AS score,
           w.path_types AS relation_path
    FROM walk w
    JOIN provision p ON p.id = w.node
    WHERE p.is_anchor_only = false                         -- B1a-03: the walk may TRAVERSE THROUGH a
                                                            -- structural section anchor (its parent_of/
                                                            -- child_of edges connect leaf→section→root in
                                                            -- the recursive step above), but a bare anchor
                                                            -- (no text) is NEVER returned as an answer node.
      AND (p.valid_to IS NULL                               -- current law: open-ended…
        OR p.valid_to > COALESCE($4::date, CURRENT_DATE))  -- …or still in force at as_of (default today)
      AND (p.valid_from IS NULL                            -- …AND already in force at as_of: a provision
        OR p.valid_from <= COALESCE($4::date, CURRENT_DATE))  -- with a FUTURE účinnost is not yet law
    ORDER BY w.node, w.score DESC                          -- keep the best-scoring path per provision
) best
ORDER BY best.score DESC
LIMIT $5
"""

# Injection point for the optional current-only filter: the unique trailing SQL comment on the
# final gate's valid_from line. four_corners appends seed.current_only_clause() right after it, so
# with the flag on the expanded set, not only the seeds, is restricted to current law. The clause
# starts with a newline and so lands on its own line; with the flag off it is empty and the SQL is
# unchanged.
_CTE_CURRENT_ONLY_ANCHOR = "-- with a FUTURE účinnost is not yet law"


# --------------------------------------------------------------------------- version-aware as-of text
# The same point-in-time version lookup as seed.py, applied to the four-corners result. The walk's
# in-force mask (the final gate in the SQL above) decides which nodes are reachable at as_of using
# the head row's valid_from/valid_to. But after an amendment the head valid_from still points at the
# provision's origin, so a past as_of can reach a node whose head text is the new wording. This
# overlay copies the text valid in the [valid_from, valid_to) window from provision_version onto each
# reachable hit's asof_text. It runs only when temporal_substrate_enabled is on and as_of is strictly
# in the past; the as-of renderer (engine._decorate, temporal_asof_render_enabled) decides separately
# whether to serve that text. Both flags default to off. Ranking is not affected.
async def _overlay_asof_text(conn, hits: "list[NeighborHit]", as_of: date | None) -> "list[NeighborHit]":
    from pythia.graph.seed import _asof_version_text, _past_as_of  # shared version lookup and guard

    if not hits or not _past_as_of(as_of) or not get_settings().temporal_substrate_enabled:
        return hits
    if not await conn.fetchval("SELECT to_regclass('public.provision_version') IS NOT NULL"):
        return hits
    texts = await _asof_version_text(conn, [h.provision_id for h in hits], as_of)
    for h in hits:
        if h.provision_id in texts:
            h.asof_text = texts[h.provision_id]
    return hits


@dataclass
class NeighborHit:
    """A provision reached by graph expansion, with its hop count, score and relation path."""

    provision_id: str
    hops: int
    score: float
    relation_path: list[str]
    # Set only when rank fusion runs: the semantic cosine score, the fused score (RRF plus the
    # protected floor) and the audit breakdown. None otherwise.
    sem_score: float | None = None
    fused_score: float | None = None
    why: dict | None = None
    # The text in force at a past as_of (mirrors seed.Seed.asof_text). Populated only when
    # temporal_substrate_enabled is on and as_of is strictly in the past; engine._decorate serves it
    # as CitedProvision.text_md when temporal_asof_render_enabled is on. Both flags default to off,
    # and the value is None for current-date queries. Which nodes are reachable at as_of is already
    # decided by the walk's in-force mask; this field only supplies the correct wording for them.
    asof_text: str | None = None


@dataclass
class RulingHit:
    """A ruling that engages a statute provision, for the opt-in rulings panel.

    Carries the display fields a panel needs (court, case number, title, date), so the UI never
    needs a second lookup: a bare ruling_id cannot be rendered on its own.
    """

    ruling_id: str  # the ruling's CASE_LAW provision row
    relation: str  # how it engages the §: applied_by, interpreted_by or unified_by
    rel_id: int
    weight: float
    label: str | None = None  # the ruling provision's label (e.g. the operative-part marker)
    case_number: str | None = None  # docket / file number (source_document.external_id)
    court: str | None = None  # issuing court (source_document.issuing_authority)
    title: str | None = None  # the ruling's title (source_document.title)
    decided_on: date | None = None  # decision date (provision.valid_from, when the ruling took effect)
    jurisdiction: str | None = None


# Parameters: $1 provision_id uuid, $2 panel rel_ids smallint[], $3 as_of date or NULL (defaults to
# the current date).
_RULINGS_PANEL_SQL = """
SELECT e.dst::text AS ruling_id, e.edge_type AS relation, e.rel_id AS rel_id,
       e.weight::double precision AS weight,
       pd.label AS label, pd.valid_from AS decided_on, pd.jurisdiction AS jurisdiction,
       sd.external_id AS case_number, sd.issuing_authority AS court, sd.title AS title
  FROM pythia_edge e
  JOIN provision pd ON pd.id = e.dst
  JOIN source_document sd ON sd.id = pd.source_document_id     -- the ruling's court / case-no / title
 WHERE e.src = $1::uuid
   AND e.rel_id = ANY($2::smallint[])                     -- inverse case-law relations (statute → ruling)
   AND pd.legal_hierarchy = 'CASE_LAW'                     -- only real ruling nodes (not dangling refs)
   AND (pd.valid_to IS NULL OR pd.valid_to > COALESCE($3::date, CURRENT_DATE))     -- in-force at as_of…
   AND (pd.valid_from IS NULL OR pd.valid_from <= COALESCE($3::date, CURRENT_DATE))  -- …already in effect
 ORDER BY e.weight DESC
"""


async def rulings_for_provision(provision_id: str, *, as_of: date | None = None) -> list[RulingHit]:
    """Return the rulings that engage one statute provision, for the opt-in rulings panel.

    Follows the inverse case-law relations interpreted_by(22), applied_by(24) and unified_by(26),
    which point from statute to ruling. This is the explicit path for showing persuasive case law
    alongside a statute answer; it is never folded into the four-corners result, because a statute
    answer must not silently contain case law. The rulings ingested so far link via 'applies',
    so their statute-side relation is applied_by, which is included.

    `as_of` applies the point-in-time mask (default: the current date), so the panel shows only
    rulings in effect on that date, consistent with the rest of the read API.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_RULINGS_PANEL_SQL, provision_id, list(PANEL_INVERSE_CASE_LAW_REL_IDS), as_of)
    return [
        RulingHit(
            str(r["ruling_id"]),
            r["relation"],
            int(r["rel_id"]),
            float(r["weight"]),
            label=r["label"],
            case_number=r["case_number"],
            court=r["court"],
            title=r["title"],
            decided_on=r["decided_on"],
            jurisdiction=r["jurisdiction"],
        )
        for r in rows
    ]


async def four_corners(
    seed_ids: list[str],
    *,
    max_hops: int = 3,
    k: int = 20,
    as_of: date | None = None,
    decay: float | None = None,
    include_case_law: bool = False,
    fanout_cap: int = 32,
    conn=None,
) -> list[NeighborHit]:
    """Walk the graph from `seed_ids` with the recursive four-corners CTE (law in force only).

    Args:
        seed_ids: Provision ids to start from. Duplicates are ignored.
        max_hops: Maximum path length.
        k: Number of reached provisions to return, best score first.
        as_of: Date at which provisions must be in force (default: the current date).
        decay: Per-hop score decay (default: ``settings.decay_base``).
        include_case_law: By default the walk excludes CASE_LAW destinations, so a statute seed
            never pulls in rulings. Set to True only on the explicit opt-in path. annuls/annulled_by
            (27/28) are always excluded, because they record status lineage, not navigation.
        fanout_cap: Maximum out-edges expanded per node per hop (the heaviest ones). This latency
            heuristic stops a definition hub (about 380 out-edges) from blowing the walk past its
            budget. Keeping the heaviest edges preserves the top k by score in the common case, but
            recall is not guaranteed: a result reachable only through a low-weight bridge into a
            high-value subtree can be dropped (rare, max_hops >= 2). The seed step keeps
            GREATEST(fanout_cap, k) edges, so the top k depth-1 nodes survive even when k exceeds
            the cap. Nodes with fewer out-edges than the cap are unaffected.
        conn: Optional asyncpg connection, so a transactional caller (for example a test that
            stages a synthetic edge in a rolled-back transaction) sees its own uncommitted rows.
            By default a pooled connection is acquired.
    """
    if not seed_ids:
        return []
    seed_ids = list(dict.fromkeys(seed_ids))  # unnest($1) runs the seed step once per id, so
    # duplicate seeds would multiply the work (not the results)
    decay = get_settings().decay_base if decay is None else decay
    args = (seed_ids, max_hops, decay, as_of, k, include_case_law, fanout_cap)
    if conn is not None:
        from pythia.graph.seed import current_only_clause  # the single shared current-only predicate

        sql = _FOUR_CORNERS_SQL.replace(
            _CTE_CURRENT_ONLY_ANCHOR, _CTE_CURRENT_ONLY_ANCHOR + await current_only_clause(conn)
        )
        rows = await conn.fetch(sql, *args)
        hits = [
            NeighborHit(str(r["provision_id"]), r["hops"], float(r["score"]), list(r["relation_path"])) for r in rows
        ]
        return await _overlay_asof_text(conn, hits, as_of)  # past as_of only, when the flag is on
    pool = await get_pool()
    async with pool.acquire() as conn:
        from pythia.graph.seed import current_only_clause  # the single shared current-only predicate

        sql = _FOUR_CORNERS_SQL.replace(
            _CTE_CURRENT_ONLY_ANCHOR, _CTE_CURRENT_ONLY_ANCHOR + await current_only_clause(conn)
        )
        rows = await conn.fetch(sql, *args)
        hits = [
            NeighborHit(str(r["provision_id"]), r["hops"], float(r["score"]), list(r["relation_path"])) for r in rows
        ]
        return await _overlay_asof_text(conn, hits, as_of)  # past as_of only, when the flag is on


async def _label_map(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT p.id, p.label, p.legal_hierarchy, p.jurisdiction, sd.title
                 FROM provision p JOIN source_document sd ON sd.id = p.source_document_id
                WHERE p.id = ANY($1::uuid[])""",
            ids,
        )
    return {str(r["id"]): dict(r) for r in rows}


async def _demo(seed: str | None) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if seed is None:
            seed = str(
                await conn.fetchval(
                    """SELECT src FROM pythia_edge WHERE edge_type = 'references'
                   GROUP BY src ORDER BY count(*) DESC LIMIT 1"""
                )
            )
        smeta = await conn.fetchrow(
            """SELECT p.label, p.jurisdiction, sd.title FROM provision p
                 JOIN source_document sd ON sd.id = p.source_document_id WHERE p.id = $1::uuid""",
            seed,
        )
    print(f"SEED  {seed}")
    if smeta:
        print(f"      {smeta['label']}  [{smeta['jurisdiction']}]  · {(smeta['title'] or '')[:70]}")
    print("\nFour-corners (max_hops=3, current law):")
    hits = await four_corners([seed], max_hops=3, k=12)
    labels = await _label_map([h.provision_id for h in hits])
    for h in hits:
        m = labels.get(h.provision_id, {})
        path = " → ".join(h.relation_path)
        lbl = m.get("label") or "?"
        title = (m.get("title") or "")[:40]
        print(f"  h{h.hops} s={h.score:.4f}  {lbl:<22} [{m.get('jurisdiction', '?')}] {title}")
        print(f"         via: {path}")
    print(f"\n{len(hits)} provisions reached behind the four corners.")
    await close_pool()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(_demo(arg))
