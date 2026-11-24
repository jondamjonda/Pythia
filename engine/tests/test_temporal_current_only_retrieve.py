"""TC-4b — current_only extended to the SEED + four-corners CTE retrieve path (additive, gated default-OFF).

TC-4 added the fail-open current_only pit predicate to NexusPort.candidates() (proven row-for-row on the
throwaway DB in test_temporal_current_only.py). TC-4b shares that SAME predicate (seed._CURRENT_ONLY_PRED via
seed.current_only_clause) across the three seed queries (sparse/dense/clause) and the four_corners final gate,
so the DEFAULT query-driven retrieve set (entry + cte expansion) is current-law-only when the flag is ON.

DB-free: the gating logic is tested with a fake conn (the column-presence probe is mockable), the four-corners
injection is a pure string test, and the predicate's fail-open shape is asserted directly. (The actual row-drop
behaviour of the predicate is already proven by TC-4's candidates throwaway-DB test — it is the SAME predicate.)
"""

from __future__ import annotations

import asyncio


import pythia.graph.cte_expand as cte
import pythia.graph.seed as seed
from pythia.config import PythiaSettings


class _FakeConn:
    """A conn whose pit_status-column probe returns `has_col` (no real DB)."""

    def __init__(self, has_col: bool):
        self._has = has_col

    async def fetchval(self, _sql, *_a):
        return self._has


def _patch(monkeypatch, *, enabled: bool):
    s = PythiaSettings()
    s.temporal_current_only_enabled = enabled
    monkeypatch.setattr(seed, "get_settings", lambda: s, raising=True)


# ── gating: OFF ⇒ "" (the byte-identical baseline) ───────────────────────────────────────────────
def test_clause_off_is_empty_without_touching_conn(monkeypatch):
    _patch(monkeypatch, enabled=False)

    # flag OFF short-circuits BEFORE the column probe — a conn that would raise proves it is never called.
    class _Boom:
        async def fetchval(self, *_a):
            raise AssertionError("column probe must not run when the flag is OFF")

    assert asyncio.run(seed.current_only_clause(_Boom())) == ""


def test_default_flag_is_off():
    assert PythiaSettings().temporal_current_only_enabled is False


# ── gating: ON + column present ⇒ the shared predicate; ON + absent ⇒ "" (column-absence-safe) ───
def test_clause_on_present_returns_shared_predicate(monkeypatch):
    _patch(monkeypatch, enabled=True)
    frag = asyncio.run(seed.current_only_clause(_FakeConn(True)))
    assert seed._CURRENT_ONLY_PRED in frag
    assert frag.strip() == seed._CURRENT_ONLY_PRED  # just the predicate (+ leading whitespace)


def test_clause_on_absent_degrades_to_empty(monkeypatch):
    _patch(monkeypatch, enabled=True)
    assert asyncio.run(seed.current_only_clause(_FakeConn(False))) == ""


def test_pit_status_col_present_reflects_the_probe():
    assert asyncio.run(seed._pit_status_col_present(_FakeConn(True))) is True
    assert asyncio.run(seed._pit_status_col_present(_FakeConn(False))) is False


# ── the predicate is FAIL-OPEN (keep IN_FORCE + NULL, drop everything else) ───────────────────────
def test_predicate_is_fail_open():
    p = seed._CURRENT_ONLY_PRED
    assert "pit_status IS NULL" in p  # keep un-lineaged (unknown) — never silently dropped
    assert "pit_status = 'IN_FORCE'" in p  # keep current
    assert "SUPERSEDED" not in p and "NOT_YET" not in p  # by exclusion: everything else (the known-stale) drops


# ── four_corners injection: OFF byte-identical; ON appends the predicate exactly once ─────────────
def test_four_corners_anchor_is_unique():
    assert cte._FOUR_CORNERS_SQL.count(cte._CTE_CURRENT_ONLY_ANCHOR) == 1


def test_four_corners_off_sql_is_byte_identical():
    off = cte._FOUR_CORNERS_SQL.replace(cte._CTE_CURRENT_ONLY_ANCHOR, cte._CTE_CURRENT_ONLY_ANCHOR + "")
    assert off == cte._FOUR_CORNERS_SQL  # OFF (clause "") is a no-op → byte-identical


def test_four_corners_on_sql_appends_predicate_after_the_gate():
    frag = "\n                  " + seed._CURRENT_ONLY_PRED
    on = cte._FOUR_CORNERS_SQL.replace(cte._CTE_CURRENT_ONLY_ANCHOR, cte._CTE_CURRENT_ONLY_ANCHOR + frag)
    assert seed._CURRENT_ONLY_PRED in on
    # the predicate sits on its OWN line immediately after the gate's comment anchor (the comment terminates it)
    assert (cte._CTE_CURRENT_ONLY_ANCHOR + frag) in on
    assert on.count(seed._CURRENT_ONLY_PRED) == 1
