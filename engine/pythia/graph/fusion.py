"""Pure rank-fusion math for re-ranking graph results with semantic similarity.

Graph expansion (CTE, PPR or GNN) returns provisions connected to the seeds, but never re-checks
them against the question. This module fuses a per-provision semantic score (a cosine similarity
computed elsewhere and passed in as `sem_scores`) with the graph order. It is a re-rank, never a
filter: Reciprocal Rank Fusion (RRF) plus a floor for protected provisions.

The key guarantee: a legally essential provision (a definition, penalty, parent or transposition)
reached by the graph with near-zero cosine can be raised by good semantics, but never pruned or
demoted below its graph-only position. Two mechanisms enforce this:

  1. RRF only adds. score(p) = w_g/(k+rg+1) + w_s/(k+rs+1), where rg and rs are the 0-based ranks
     in graph order and semantic order. The graph term depends only on rg, and the semantic term is
     always positive (an id without a cosine is ranked just past the end). Adding the semantic lane
     can therefore only raise a provision, never push it below `graph_rank_score(rg)`.
  2. The protected floor makes this explicit: for every protected (or PPR) provision,
     final = max(fused, graph_rank_score(graph rank)). Given point 1 this is usually a no-op; it
     makes the guarantee unconditional.

The module is pure: no database, I/O, torch, clock or randomness. It lives in graph/ so it may
import pythia.graph.relations for the protected-relation registry. The cosine lane is
deterministic; the cross-encoder lane is advisory and off by default (`ce_enabled`), so the default
path is bit-for-bit reproducible. Every sort breaks ties by provision_id.
"""

from __future__ import annotations

from dataclasses import dataclass

from pythia.graph import relations


@dataclass(frozen=True)
class FusionParams:
    """Tunable parameters for the graph plus semantic RRF re-rank."""

    w_g: float = 1.0  # graph-lane weight (the structural prior; dominant by design)
    w_s: float = 0.6  # semantic-lane weight for the cte and ppr methods
    w_s_gnn: float = 0.3  # semantic weight for method='gnn' (halved: the GNN already learns relevance)
    k_rrf: int = 60  # RRF rank-damping constant (the customary 60)
    M: int = 16  # re-rank window: how many top hits the cross-encoder, if enabled, may rescore
    ce_enabled: bool = False  # advisory cross-encoder lane; off keeps fusion deterministic


# Protected relation ids, matched against the last hop that reached a provision. A provision reached
# this way is legally essential and gets the floor: parent_of/child_of (3/4, structural context),
# defines/uses_term (5/6, binding definitions), transposes/transposed_by (7/8, EU to Czech
# implementation).
PROTECTED_REL_IDS = frozenset({3, 4, 5, 6, 7, 8})

# Protected role ids, matched against the provision's own analysis_roles: definition (1),
# obligation (2), prohibition (3) and sanction (7), the rules a compliance answer cannot omit.
PROTECTED_ROLE_IDS = frozenset({1, 2, 3, 7})


def is_protected(relation_path: list[str], analysis_roles: list[int] | None, *, is_rule: bool = False) -> bool:
    """Return True if the provision is legally essential and must keep at least its graph position.

    A provision is protected when any of these holds:

    - `is_rule` is set. This is the resolved trust rule kind (``resolve_trust(...).is_rule``), the
      authoritative signal for a binding duty, prohibition or sanction (section-level kind,
      per-provision override or cue). It is checked in addition to the cue-based roles because the
      cue classifier alone is approximate; a connected rule-kind provision must always surface.
    - The last label in `relation_path` maps to a relation whose rel_id is in PROTECTED_REL_IDS.
      The lookup uses ``BY_LABEL.get`` because 'seed' and '' are not registry labels.
    - `analysis_roles` intersects PROTECTED_ROLE_IDS.

    An empty or seed-only path with no protected role and `is_rule` false is not protected. Such a
    provision still cannot fall below its graph position (RRF only adds); it is just not pinned by
    the explicit floor.
    """
    if is_rule:
        return True
    if relation_path:
        last_rel = relations.BY_LABEL.get(relation_path[-1])  # .get: 'seed' and '' are not labels
        if last_rel is not None and last_rel.rel_id in PROTECTED_REL_IDS:
            return True
    return bool(set(analysis_roles or []) & PROTECTED_ROLE_IDS)


def graph_rank_score(graph_rank: int, params: FusionParams, *, w_g: float | None = None) -> float:
    """Return the graph-only RRF score at 0-based `graph_rank`: w_g / (k + rank + 1).

    Protected provisions are pinned at or above this value, and `rrf_fuse` adds the semantic term
    to it (never subtracts).
    """
    weight = params.w_g if w_g is None else w_g
    return weight / (params.k_rrf + graph_rank + 1)


def rrf_fuse(
    graph_order: list[str],
    sem_order: list[str],
    params: FusionParams,
    *,
    w_s: float,
) -> dict[str, float]:
    """Fuse two ordered id lists with Reciprocal Rank Fusion and return ``{id: fused_score}``.

    Each id in `graph_order` scores w_g/(k+rg+1) + w_s/(k+rs+1), where rg and rs are its 0-based
    ranks in `graph_order` and `sem_order`. An id missing from `sem_order` is treated as ranked just
    past its end, so its semantic term is small but still positive and deterministic. Because the
    graph term does not depend on the semantic lane, the fused score is always at least
    graph_rank_score(rg).
    """
    sem_rank = {pid: i for i, pid in enumerate(sem_order)}
    sem_miss = len(sem_order)  # rank for ids without a cosine: just past the end
    fused: dict[str, float] = {}
    for rg, pid in enumerate(graph_order):
        rs = sem_rank.get(pid, sem_miss)
        fused[pid] = params.w_g / (params.k_rrf + rg + 1) + w_s / (params.k_rrf + rs + 1)
    return fused


def apply_floor(
    fused: dict[str, float],
    graph_ranks: dict[str, int],
    protected_ids: set[str],
    params: FusionParams,
    *,
    w_s_for_method: float | None = None,  # noqa: ARG001  unused, see docstring
) -> dict[str, float]:
    """Pin every protected id at or above its graph-only score and return a new dict.

    For protected ids, final = max(fused[id], graph_rank_score(graph_ranks[id])); other ids pass
    through unchanged and the input is not mutated. Because RRF only adds, this is normally a no-op;
    it makes the guarantee that an essential provision never sinks below its graph rank explicit
    and unconditional.

    `w_s_for_method` has no effect: the floor depends only on w_g and the graph rank, never on the
    semantic weight. The parameter is kept, with a default, so existing callers and tests keep
    working; it can be removed once they are updated together.
    """
    floored = dict(fused)
    for pid in protected_ids:
        if pid not in floored:
            continue
        seat = graph_rank_score(graph_ranks[pid], params)
        if seat > floored[pid]:
            floored[pid] = seat
    return floored


def fuse(
    hits: list,
    sem_scores: dict[str, float],
    roles_by_id: dict[str, list[int] | None],
    *,
    method: str,
    params: FusionParams | None = None,
    ppr_ids: set[str] | None = None,
    is_rule_by_id: dict[str, bool] | None = None,
) -> list:
    """Re-rank graph hits by fusing their graph order with semantic similarity.

    Pure apart from mutating the hits: each NeighborHit gets ``sem_score``, ``fused_score`` and
    ``why`` set in place. Returns the hits sorted by fused score (descending), ties broken by
    provision_id; empty `hits` returns an empty list.

    Args:
        hits: NeighborHit objects from graph expansion.
        sem_scores: Cosine similarity per provision id. A missing id sinks to the bottom of the
            semantic lane.
        roles_by_id: The provision's analysis_roles, used for protection.
        method: Expansion method; 'gnn' uses ``w_s_gnn`` as the semantic weight, others ``w_s``.
        params: Fusion parameters (defaults to ``FusionParams()``).
        ppr_ids: Ids that are always protected (every PPR hit gets the floor).
        is_rule_by_id: Resolved trust rule kind per id; an id mapped to True is protected even
            without a protected relation or role.
    """
    if not hits:
        return []

    params = params or FusionParams()
    ppr_ids = ppr_ids or set()
    is_rule_by_id = is_rule_by_id or {}
    w_s = params.w_s_gnn if method == "gnn" else params.w_s

    hit_by_id = {h.provision_id: h for h in hits}
    ids = [h.provision_id for h in hits]

    # Graph order: structural score descending, ties broken by id, for deterministic ranks.
    graph_order = sorted(ids, key=lambda pid: (-hit_by_id[pid].score, pid))
    graph_ranks = {pid: i for i, pid in enumerate(graph_order)}

    # Semantic order: cosine descending; ids without a cosine sort last (-inf), ties broken by id.
    sem_order = sorted(ids, key=lambda pid: (-sem_scores.get(pid, float("-inf")), pid))
    sem_ranks = {pid: i for i, pid in enumerate(sem_order)}

    fused = rrf_fuse(graph_order, sem_order, params, w_s=w_s)

    protected_ids = {
        pid
        for pid in ids
        if pid in ppr_ids
        or is_protected(hit_by_id[pid].relation_path, roles_by_id.get(pid), is_rule=bool(is_rule_by_id.get(pid, False)))
    }

    floored = apply_floor(fused, graph_ranks, protected_ids, params, w_s_for_method=w_s)

    for h in hits:
        pid = h.provision_id
        h.sem_score = sem_scores.get(pid)
        h.fused_score = floored[pid]
        h.why = {
            "structural": h.score,
            "sem": sem_scores.get(pid),
            "graph_rank": graph_ranks[pid],
            "sem_rank": sem_ranks[pid],
            "fused": floored[pid],
            "protected": pid in protected_ids,
            "lane": method,
        }

    return sorted(
        hits,
        key=lambda h: (-(h.fused_score if h.fused_score is not None else h.score), h.provision_id),
    )
