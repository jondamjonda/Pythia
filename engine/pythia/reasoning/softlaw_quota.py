"""Cap on how much of the final top-k soft-law guidance may occupy.

Soft-law guidance (official notices, methodological guidelines, interpretive opinions: úřední
sdělení, metodické pokyny, výkladová stanoviska) is useful context, but it must not push binding
statute out of the top-k shown to the user. On guidance-heavy topics a plain RRF ranking can fill
the top-k with guidance and bury the binding provision. This module limits the soft-law share of
the top-k to ``max_soft`` (default ``ceil(k/3)``) by moving the excess soft-law hits below the
binding ones. It only reorders: no binding provision is ever dropped, and because the caller passes
a candidate pool of at least k, no soft-law provision that would have fitted is dropped either.

Pure and synchronous: takes a ranked list of ids and an ``{id: bindingness}`` map and returns a
reordered list. The caller checks ``config.softlaw_quota_enabled`` (off by default) and skips this
module entirely when it is off.
"""

from __future__ import annotations

from math import ceil

# Bindingness value the corpus stores on soft-law source documents
# (``ports.ProvisionRow.bindingness`` or ``meta['bindingness']``). Uppercase, unlike the derived
# hyphenated trust tier.
SOFT_LAW = "SOFT_LAW"


def default_max_soft(k: int) -> int:
    """Return the default soft-law budget for a top-k, ``ceil(k/3)``.

    This is at least 1 for any ``k >= 1``, so one strong piece of guidance can still surface, and 0
    only for ``k <= 0``.
    """
    return ceil(k / 3) if k > 0 else 0


def _is_soft(bindingness: str | None) -> bool:
    """Return True if the bindingness marks soft law.

    Missing bindingness counts as not soft, so an untagged provision is never demoted as if it were
    mere guidance.
    """
    return bindingness == SOFT_LAW


def apply_softlaw_quota(
    ranked: list[str], bindingness: dict[str, str | None], k: int, max_soft: int | None = None
) -> list[str]:
    """Reorder ``ranked`` so that at most ``max_soft`` of the first k ids are soft law.

    ``ranked`` holds ids in descending relevance. Guarantees:
      * No id is dropped; the result is a permutation of ``ranked``.
      * Binding ids keep their relative order and never move down, so a binding id that was in the
        top-k stays there.
      * Soft-law ids beyond ``max_soft`` within the first k are appended after the kept ids, in
        their original relative order.

    ``max_soft`` defaults to ``ceil(k/3)``. Idempotent.
    """
    if max_soft is None:
        max_soft = default_max_soft(k)
    if k <= 0 or not ranked:
        return list(ranked)

    kept: list[str] = []  # ids that keep (or gain) their place, in relevance order
    demoted: list[str] = []  # excess soft-law ids moved below, in relevance order
    soft_in_topk = 0

    for i, pid in enumerate(ranked):
        soft = _is_soft(bindingness.get(pid))
        # Only the window that fills the final top-k is capped. While ``kept`` has fewer than k ids
        # we are still filling that window: a soft-law hit beyond the budget is demoted, a binding
        # hit is always kept.
        if soft and len(kept) < k and soft_in_topk >= max_soft:
            demoted.append(pid)
            continue
        if soft and len(kept) < k:
            soft_in_topk += 1
        kept.append(pid)

    return kept + demoted
