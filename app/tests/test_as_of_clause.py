"""B2a-09 (G2/INV-7) — the L1 point-in-time `as_of` filter clause.

Pure tests over `metadata_filter_sql`: an explicit `as_of` OWNS the temporal window and SUPERSEDES
`active_only` (no double-filter); `as_of=None` (default) is byte-identical to today's behaviour. The
anchor-exclusion + end-to-end threading are covered by the live L1 path; here we pin the SQL composition.
"""

from datetime import date

from regsuper.retrieval.hybrid import RetrievalFilters, metadata_filter_sql


def test_default_as_of_none_is_byte_stable_active_only():
    """No as_of → the existing active_only CURRENT_DATE clause, unchanged. No as_of bind param."""
    where, params = metadata_filter_sql(RetrievalFilters(active_only=True))
    assert "CURRENT_DATE" in where
    assert "f_as_of" not in params
    assert ":f_as_of" not in where


def test_as_of_supersedes_active_only():
    """An explicit as_of replaces the CURRENT_DATE clause with the valid_from<=as_of<valid_to window —
    NOT both (no double-filter), even when active_only is also True."""
    where, params = metadata_filter_sql(RetrievalFilters(active_only=True, as_of=date(2020, 1, 1)))
    assert "CURRENT_DATE" not in where  # active_only's clause is superseded
    assert "p.valid_from IS NULL OR p.valid_from <= :f_as_of" in where
    assert "p.valid_to IS NULL OR p.valid_to > :f_as_of" in where
    assert params["f_as_of"] == date(2020, 1, 1)


def test_no_temporal_clause_when_neither():
    """active_only=False and no as_of → no temporal clause at all (the tier-3 widen, today's behaviour)."""
    where, params = metadata_filter_sql(RetrievalFilters(active_only=False))
    assert "CURRENT_DATE" not in where and "f_as_of" not in params


def test_as_of_applies_even_when_active_only_false():
    """PIT must hold regardless of active_only — a tier-3 widen drops jurisdiction/sector, never the
    temporal window. as_of present + active_only=False still yields the as_of window."""
    where, params = metadata_filter_sql(RetrievalFilters(active_only=False, as_of=date(2019, 6, 1)))
    assert ":f_as_of" in where and params["f_as_of"] == date(2019, 6, 1)
