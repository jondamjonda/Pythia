"""VL-F-14 — the gap_backlog ranked rollup (gap_backlog.py) + a shadow decree_absent derive→rollup fixture.

Proves the STABLE ordering contract (count DESC, then canonical-index ASC), that non-backlog codes are
dropped, and an end-to-end-ish path: a demoted delegating-provision claim → derive_gap_kind → rollup ranks
`decree_absent`.
"""

from __future__ import annotations

from pythia.reasoning.gap_backlog import BacklogEntry, as_dicts, rollup, total
from pythia.reasoning.gap_kind import GAP_KIND_BACKLOG_CODES, GapKind
from pythia.reasoning.gap_kind_derive import GapSignals, derive_gap_kind


def _codes(entries):
    return [e.gap_kind for e in entries]


def test_rollup_ranks_by_count_desc():
    stream = [GapKind.NO_FACT_ROW.value] * 3 + [GapKind.DECREE_ABSENT.value] * 5 + [GapKind.AUTHORITY_LOCATOR.value] * 1
    out = rollup(stream)
    assert _codes(out) == [GapKind.DECREE_ABSENT.value, GapKind.NO_FACT_ROW.value, GapKind.AUTHORITY_LOCATOR.value]
    assert [e.count for e in out] == [5, 3, 1]


def test_tie_break_is_canonical_index_not_alphabetical():
    # equal counts ⇒ order by canonical position in GAP_KIND_BACKLOG_CODES (no_fact_row=0 precedes
    # decree_absent=2 precedes form_portal=5), NOT alphabetically (which would put decree_absent first).
    stream = [GapKind.FORM_PORTAL.value, GapKind.DECREE_ABSENT.value, GapKind.NO_FACT_ROW.value]
    out = rollup(stream)
    assert _codes(out) == [GapKind.NO_FACT_ROW.value, GapKind.DECREE_ABSENT.value, GapKind.FORM_PORTAL.value]


def test_rollup_is_deterministic():
    stream = [GapKind.SOFT_LAW_ABSENT.value, GapKind.SOFT_LAW_ABSENT.value, GapKind.NO_TEXT_SUPPORT.value]
    assert rollup(stream) == rollup(list(reversed(stream)))


def test_non_backlog_codes_dropped():
    # UNKNOWN / structural codes are NOT connector-queue work ⇒ ignored, never raise.
    stream = [GapKind.UNKNOWN.value, GapKind.OUT_OF_SCOPE.value, "garbage", "", GapKind.NO_FACT_ROW.value]
    out = rollup(stream)
    assert _codes(out) == [GapKind.NO_FACT_ROW.value]
    assert total(out) == 1


def test_empty_and_all_unknown_yield_empty():
    assert rollup([]) == []
    assert rollup(["nope", GapKind.UNKNOWN.value]) == []


def test_as_dicts_and_total():
    out = rollup([GapKind.DECREE_ABSENT.value] * 2 + [GapKind.NO_FACT_ROW.value])
    assert as_dicts(out) == [
        {"gap_kind": GapKind.DECREE_ABSENT.value, "count": 2},
        {"gap_kind": GapKind.NO_FACT_ROW.value, "count": 1},
    ]
    assert total(out) == 3


def test_every_backlog_entry_code_is_canonical():
    out = rollup([c for c in GAP_KIND_BACKLOG_CODES])
    assert all(isinstance(e, BacklogEntry) and e.gap_kind in GAP_KIND_BACKLOG_CODES for e in out)


# ── shadow decree_absent e2e-ish: demoted delegating claim → derive → rollup ranks decree_absent ────────────
def test_shadow_decree_absent_derive_then_rollup():
    # A batch of demoted claims as the enforce demote-site would produce them: two delegating provisions whose
    # implementing decree is NOT in corpus (rel-11 present, rel-12 absent) + one authority-pointer slot.
    demoted_signals = [
        GapSignals(slot="obligations", is_delegating=True, has_implements_edge=False),
        GapSignals(slot="obligations", is_delegating=True, has_implements_edge=False),
        GapSignals(slot="where"),
    ]
    kinds = [derive_gap_kind(s) for s in demoted_signals]
    assert kinds == [GapKind.DECREE_ABSENT.value, GapKind.DECREE_ABSENT.value, GapKind.AUTHORITY_LOCATOR.value]
    backlog = rollup(kinds)
    # decree_absent tops the backlog (2 vs 1) — the formal signal the decree queue (step 92) consumes.
    assert backlog[0] == BacklogEntry(gap_kind=GapKind.DECREE_ABSENT.value, count=2)
