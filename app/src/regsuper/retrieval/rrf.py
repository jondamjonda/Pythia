"""Reciprocal Rank Fusion (RRF) of several ranked result sets.

Cormack, Clarke & Buettcher (2009), "Reciprocal Rank Fusion outperforms Condorcet
and individual rank learning methods". The combined score for a document d is

    RRF_score(d) = sum over i of 1 / (k + rank_i(d))

where rank_i is d's 1-based rank in result set i (a set that does not contain d
contributes nothing) and k is a constant that flattens differences in the very top
ranks. Cormack's paper uses k=60; we follow that default.

Why RRF instead of a learned weighting:
  - it needs only ranks, never raw scores, so the dense (cosine) and sparse
    (ts_rank_cd) sets combine without normalisation;
  - it degrades gracefully when one search returns junk: a result present in only
    one set still gets a finite score, but loses to documents that rank high in
    several sets;
  - it is deterministic, which matters for replay and audit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from regsuper.retrieval.hybrid import SearchHit


@dataclass(frozen=True)
class FusedHit:
    """One row of the RRF output.

    `score` is the summed reciprocal-rank contribution. `contributions` lists the
    per-source ranks that fed in, which explains "why did this provision win?" in
    the debug UI.
    """

    provision_id: UUID
    score: float
    contributions: tuple[tuple[str, int], ...]  # ((source, rank), ...)


def reciprocal_rank_fusion(
    result_sets: Iterable[Sequence[SearchHit]],
    *,
    k: int = 60,
    top_n: int | None = None,
) -> list[FusedHit]:
    """Merge any number of ranked SearchHit lists into a single ranking.

    `result_sets` is an iterable of already-sorted hit lists: each list's order is
    its ranking, and the `score` field on SearchHit is ignored. A provision missing
    from a set gets no contribution from it.
    """
    if k <= 0:
        raise ValueError("RRF constant k must be positive (Cormack 2009 uses 60).")

    fused: dict[UUID, float] = {}
    contributions: dict[UUID, list[tuple[str, int]]] = {}

    for hits in result_sets:
        seen_in_set: set[UUID] = set()
        for rank0, hit in enumerate(hits):
            # First (highest-scoring) occurrence of an id in a single set wins;
            # duplicates within one set don't double-count.
            if hit.provision_id in seen_in_set:
                continue
            seen_in_set.add(hit.provision_id)

            rank1 = rank0 + 1
            fused[hit.provision_id] = fused.get(hit.provision_id, 0.0) + 1.0 / (k + rank1)
            contributions.setdefault(hit.provision_id, []).append((hit.source, rank1))

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    if top_n is not None:
        ordered = ordered[: max(0, int(top_n))]

    return [
        FusedHit(
            provision_id=pid,
            score=score,
            contributions=tuple(contributions[pid]),
        )
        for pid, score in ordered
    ]
