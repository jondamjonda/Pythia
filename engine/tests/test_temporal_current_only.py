"""TC-4 — the CURRENT-ONLY filter proof (BEHAVIOUR-CHANGING, gated, additive).

The last consumer in the temporal wave. It uses the TMP-02 per-provision pit_status to drop KNOWN-not-current
provisions from the metadata-filtered candidate feed (NexusPort.candidates) — the per-provision precision the
doc-level valid_from/valid_to DATE mask cannot express (a head whose valid_from stays at the §-origin after an
amend passes the date mask, yet pit_status='SUPERSEDED' marks it not current).

Proves the TC-4 invariants:

  (a) flag defaults OFF      — PythiaSettings().temporal_current_only_enabled is False.
  (b) OFF byte-identical     — _current_only_filter(conn) returns "" with the flag OFF (no pit predicate ⇒ the
      candidates() WHERE is byte-identical to baseline); candidates() OFF returns the FULL date-mask set,
      including the SUPERSEDED / NOT_YET_IN_FORCE provisions the date mask lets through.
  (c) ON fail-open filters   — flag ON ⇒ candidates() KEEPS IN_FORCE and NULL (un-lineaged ⇒ unknown ⇒ never
      dropped) and EXCLUDES only the KNOWN-not-current SUPERSEDED / NOT_YET_IN_FORCE; SELECT/ORDER (centrality
      desc) and every other facet of the surviving rows is unchanged.
  (d) column-absence-safe    — flag ON on a `provision` schema LACKING pit_status degrades to the byte-identical
      OFF WHERE (filter == "", _pit_status_col_present False) and candidates() does NOT crash.

The DB-touching proofs reuse the EXACT hard-guarded throwaway harness (test_amend_proof._connect_throwaway +
test_temporal_substrate._redirect_pool_to_throwaway): they connect ONLY to the throwaway 'pythia_amendtest' DB
and ABORT if current_database() is pythia/regsuper. With no throwaway DB they SKIP cleanly — never live.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

import pythia.ports as ports
from pythia.config import PythiaSettings
from tests.test_amend_proof import _FORBIDDEN_DBS, _connect_throwaway, _throwaway_dsn
from tests.test_temporal_consumer import _SETUP_SQL, _SETUP_SQL_NO_PIT, _h
from tests.test_temporal_substrate import _redirect_pool_to_throwaway


def _patch_current_only(monkeypatch, enabled: bool) -> None:
    """Make ports see temporal_current_only_enabled=`enabled`. _current_only_filter calls the module-level
    get_settings(); patch THAT binding (the TC-1/TC-2 test pattern)."""
    s = PythiaSettings()
    s.temporal_current_only_enabled = enabled
    monkeypatch.setattr(ports, "get_settings", lambda: s, raising=True)


async def _seed4(conn, *, with_pit: bool) -> dict[str, str]:
    """One doc + four provisions that ALL pass the date mask (valid_from in the past, valid_to NULL/open) and
    differ ONLY in pit_status — isolating TC-4's per-provision effect:
      A — pit IN_FORCE          (kept ON)
      B — pit SUPERSEDED        (excluded ON; date mask alone would keep it — the head is closed)
      D — pit NOT_YET_IN_FORCE  (excluded ON)
      E — pit NULL/un-lineaged  (KEPT ON — fail-open: unknown is never dropped)
    Centrality A>B>D>E so the OFF order is deterministic. with_pit=False ⇒ the column does not exist."""
    doc = uuid4()
    await conn.execute(
        "INSERT INTO source_document (id, external_id, title, bindingness, is_authoritative, temporal_status,"
        " legal_domain) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        doc,
        "TC4/0001",
        "Test Act",
        "HARD_LAW_ERGA_OMNES",
        True,
        "in-force",
        ["capital_markets"],
    )
    a, b, d, e = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [
        (a, "§ 1", "obligation A in force", date(2020, 1, 1), 9.0, "IN_FORCE"),
        (b, "§ 2", "obligation B superseded head", date(2019, 1, 1), 8.0, "SUPERSEDED"),
        (d, "§ 3", "obligation D not yet", date(2018, 1, 1), 7.0, "NOT_YET_IN_FORCE"),
        (e, "§ 4", "obligation E unlineaged", date(2020, 1, 1), 6.0, None),
    ]
    base_cols = (
        "id, source_document_id, label, text, text_hash, jurisdiction, legal_hierarchy, "
        "analysis_roles, section_kind, valid_from, valid_to, centrality"
    )
    if with_pit:
        cols = base_cols + ", pit_status"
        for pid, lbl, txt, vf, cen, pit in rows:
            await conn.execute(
                f"INSERT INTO provision ({cols}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)",
                pid,
                doc,
                lbl,
                txt,
                _h(txt),
                "CZ",
                "STATUTE",
                [2],
                "operative",
                vf,
                None,
                cen,
                pit,
            )
    else:
        cols = base_cols
        for pid, lbl, txt, vf, cen, _pit in rows:
            await conn.execute(
                f"INSERT INTO provision ({cols}) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
                pid,
                doc,
                lbl,
                txt,
                _h(txt),
                "CZ",
                "STATUTE",
                [2],
                "operative",
                vf,
                None,
                cen,
            )
    return {"A": str(a), "B": str(b), "D": str(d), "E": str(e)}


# ─────────────────────────────────────────────────────────── (a) flag defaults OFF (pure, no DB)


def test_current_only_flag_defaults_off():
    assert PythiaSettings().temporal_current_only_enabled is False


# ─────────────────────────────────────────────────────────── (b) OFF byte-identical fragment + full set


async def test_off_filter_fragment_is_empty(monkeypatch):
    """With the flag OFF, _current_only_filter must be the empty string on BOTH a pit-present and a pit-absent
    schema (⇒ the candidates() WHERE is byte-identical to baseline)."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-4 OFF-fragment proof skipped by design (never runs on live)")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        assert await conn.fetchval("SELECT current_database()") not in _FORBIDDEN_DBS
        await conn.execute(_SETUP_SQL)
        _patch_current_only(monkeypatch, False)
        assert await ports._current_only_filter(conn) == ""  # OFF on a pit-PRESENT schema
        await conn.execute(_SETUP_SQL_NO_PIT)
        assert await ports._current_only_filter(conn) == ""  # OFF on a pit-ABSENT schema


async def test_candidates_off_returns_full_date_mask_set(monkeypatch):
    """OFF: candidates() returns ALL four provisions (the date mask passes every one) in centrality-desc order —
    the SUPERSEDED and NOT_YET ones are NOT filtered (that is exactly what TC-4 ON adds)."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-4 OFF set proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        await conn.execute(_SETUP_SQL)
        ids = await _seed4(conn, with_pit=True)
    async with _redirect_pool_to_throwaway(dsn):
        _patch_current_only(monkeypatch, False)
        off = await ports.NexusPort().candidates(jurisdiction=["CZ"], limit=50)
    assert [r.id for r in off] == [ids["A"], ids["B"], ids["D"], ids["E"]]


# ─────────────────────────────────────────────────────────── (c) ON fail-open: drop known-stale, keep IN_FORCE+NULL


async def test_candidates_on_excludes_known_not_current_keeps_inforce_and_null(monkeypatch):
    """ON: the fragment fires — keep IN_FORCE (A) and NULL/un-lineaged (E), exclude SUPERSEDED (B) and
    NOT_YET_IN_FORCE (D). Order is the centrality-desc subsequence; the kept rows are byte-equal to their OFF
    selves (only the SET shrinks)."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-4 ON filter proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        await conn.execute(_SETUP_SQL)
        ids = await _seed4(conn, with_pit=True)
        assert await ports._pit_status_col_present(conn) is True
        _patch_current_only(monkeypatch, True)
        assert "pit_status" in (await ports._current_only_filter(conn))  # fragment fires
    async with _redirect_pool_to_throwaway(dsn):
        _patch_current_only(monkeypatch, False)
        off = await ports.NexusPort().candidates(jurisdiction=["CZ"], limit=50)
    async with _redirect_pool_to_throwaway(dsn):
        _patch_current_only(monkeypatch, True)
        on = await ports.NexusPort().candidates(jurisdiction=["CZ"], limit=50)

    assert [r.id for r in on] == [ids["A"], ids["E"]]  # fail-open: IN_FORCE + NULL kept
    assert ids["B"] not in [r.id for r in on]  # SUPERSEDED dropped
    assert ids["D"] not in [r.id for r in on]  # NOT_YET dropped
    # the surviving rows are unchanged vs OFF (TC-4 changes the SET, not the row content / order among survivors)
    off_by = {r.id: r for r in off}
    for r in on:
        o = off_by[r.id]
        assert (r.id, r.label, r.jurisdiction, r.legal_hierarchy, r.text) == (
            o.id,
            o.label,
            o.jurisdiction,
            o.legal_hierarchy,
            o.text,
        )


# ─────────────────────────────────────────────────────────── (d) column-absence-safe degrade


async def test_flag_on_column_absent_degrades_no_crash(monkeypatch):
    """Flag ON on a schema LACKING pit_status: _pit_status_col_present False, _current_only_filter "", and
    candidates() returns the full set with NO crash (byte-identical OFF behaviour)."""
    probe = await _connect_throwaway()
    if probe is None:
        pytest.skip("no throwaway DB — TC-4 column-absence proof skipped by design")
    await probe.close()
    dsn = _throwaway_dsn()
    async with _redirect_pool_to_throwaway(dsn) as pool, pool.acquire() as conn:
        await conn.execute(_SETUP_SQL_NO_PIT)
        ids = await _seed4(conn, with_pit=False)
        _patch_current_only(monkeypatch, True)
        assert await ports._pit_status_col_present(conn) is False
        assert await ports._current_only_filter(conn) == ""
    async with _redirect_pool_to_throwaway(dsn):
        _patch_current_only(monkeypatch, True)
        on = await ports.NexusPort().candidates(jurisdiction=["CZ"], limit=50)  # must NOT raise
    assert {r.id for r in on} == {ids["A"], ids["B"], ids["D"], ids["E"]}
