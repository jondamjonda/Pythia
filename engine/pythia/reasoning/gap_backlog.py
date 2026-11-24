"""Gap backlog: demoted-claim gap kinds counted and ranked by how often they block answers.

When a claim is demoted, gap_kind_derive.py assigns it a specific ``gap_kind``. Aggregated over one
or many runs, those codes answer "which missing class of source material blocks the most answers?"
and feed the queue of decrees and connectors to add next. This module is that rollup: a pure,
deterministic count with a stable ranking, so the operator (and the app's ``practical.gap_backlog``
telemetry) always sees the same order for the same input.

Ordering, so that equal counts never swap places between runs:
  * primary: count, descending (the most frequently missing class first);
  * secondary: the code's index in ``gap_kind.GAP_KIND_BACKLOG_CODES``, ascending (a fixed position
    from the enum, not alphabetical order).

Codes outside the eight backlog codes are ignored, because a structural, resolution or unknown gap
is not connector work. Unknown strings never raise; they are simply not counted.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from pythia.reasoning.gap_kind import GAP_KIND_BACKLOG_CODES, is_backlog_code

# Fixed position of each backlog code (its index in the enum tuple); the secondary sort key.
_CODE_ORDER: dict[str, int] = {code: i for i, code in enumerate(GAP_KIND_BACKLOG_CODES)}


@dataclass(frozen=True)
class BacklogEntry:
    """One ranked backlog row: a backlog gap kind and how many demoted claims mapped to it."""

    gap_kind: str
    count: int


def rollup(gap_kinds: Iterable[str]) -> list[BacklogEntry]:
    """Rank per-claim gap-kind strings into a stable backlog.

    Only the eight backlog codes (``gap_kind.is_backlog_code``) are counted; anything else is
    dropped. Entries are ordered by count descending, then by enum position. An empty or
    all-unknown input returns ``[]``. Never raises.
    """
    counts: Counter[str] = Counter()
    for code in gap_kinds:
        c = str(code or "")
        if is_backlog_code(c):
            counts[c] += 1
    # Count descending, then the code's enum position (stable, not alphabetical).
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], _CODE_ORDER[kv[0]]))
    return [BacklogEntry(gap_kind=code, count=n) for code, n in ordered]


def as_dicts(entries: list[BacklogEntry]) -> list[dict]:
    """Serialise a ranked backlog to plain ``{gap_kind, count}`` dicts for telemetry and audit."""
    return [{"gap_kind": e.gap_kind, "count": e.count} for e in entries]


def total(entries: list[BacklogEntry]) -> int:
    """Return the total number of demoted claims across all backlog entries."""
    return sum(e.count for e in entries)
