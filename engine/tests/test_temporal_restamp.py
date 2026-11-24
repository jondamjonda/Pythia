"""AQ-04 — pure, DB-free unit tests for the doc-level temporal re-stamp job.

Two surfaces are pinned:
  • taxonomy._temporal_status boundary behaviour (the SINGLE source of truth the job reuses) — the exact
    valid_from=2026-07-01 crossing that mislabels the ~21 SME acts, plus REPEALED and idempotence;
  • plan_restamp / apply_restamp over a MOCKED async session returning known rows — the correct diff set,
    the as_of purity (no clock read), and idempotence (apply → re-plan yields []).

No live DB is touched: the session is a tiny fake exposing async fetch()/execute().
"""

from __future__ import annotations

from datetime import date

import pytest

from pythia.graph.taxonomy import _temporal_status
from pythia.ingest.temporal_restamp import _format_plan, apply_restamp, plan_restamp


# --------------------------------------------------------------------------------------------------
# _temporal_status boundary cases — the canonical rule the job reuses (do NOT re-implement elsewhere).
# --------------------------------------------------------------------------------------------------


def test_valid_from_tomorrow_is_not_yet_effective():
    # valid_from = 2026-07-01, as_of = 2026-06-30 → the day BEFORE effect → NOT_YET_EFFECTIVE.
    assert _temporal_status(date(2026, 7, 1), None, date(2026, 6, 30)) == "NOT_YET_EFFECTIVE"


def test_valid_from_today_is_in_force():
    # SAME doc, as_of = 2026-07-02 (today) → effect date has passed → IN_FORCE. This is the exact
    # crossing that leaves the ~21 SME acts (455/1991, 187/2006, 435/2004, …) stale in the column.
    assert _temporal_status(date(2026, 7, 1), None, date(2026, 7, 2)) == "IN_FORCE"


def test_valid_from_equals_as_of_is_in_force():
    # Boundary: valid_from == as_of. The rule is valid_from > ref → NOT_YET, so equality → IN_FORCE.
    assert _temporal_status(date(2026, 7, 1), None, date(2026, 7, 1)) == "IN_FORCE"


def test_valid_to_in_past_is_repealed():
    assert _temporal_status(date(2000, 1, 1), date(2020, 1, 1), date(2026, 7, 2)) == "REPEALED"


def test_valid_to_equals_as_of_is_repealed():
    # Boundary: valid_to == as_of → valid_to <= ref → REPEALED (end date reached).
    assert _temporal_status(date(2000, 1, 1), date(2026, 7, 2), date(2026, 7, 2)) == "REPEALED"


def test_no_dates_is_in_force():
    assert _temporal_status(None, None, date(2026, 7, 2)) == "IN_FORCE"


# --------------------------------------------------------------------------------------------------
# A minimal fake async session (no DB): records fetch rows + captures UPDATE calls for assertions.
# --------------------------------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple] = []

    async def fetch(self, _sql):
        return self._rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        # Mirror the write back into our in-memory rows so a subsequent plan() sees the applied value.
        _id, new_status = args
        for r in self._rows:
            if r["id"] == _id:
                r["temporal_status"] = new_status


def _row(id_, ref, vf, vt, status):
    return {"id": id_, "external_id": ref, "title": ref, "valid_from": vf, "valid_to": vt, "temporal_status": status}


@pytest.mark.asyncio
async def test_plan_restamp_returns_only_changed_rows():
    rows = [
        # stale: stored NOT_YET_EFFECTIVE but its valid_from has passed at as_of → should flip to IN_FORCE.
        _row(1, "455/1991", date(2026, 7, 1), None, "NOT_YET_EFFECTIVE"),
        # already correct IN_FORCE → NOT in the diff.
        _row(2, "89/2012", date(2014, 1, 1), None, "IN_FORCE"),
        # stale: stored IN_FORCE but past its valid_to → should flip to REPEALED.
        _row(3, "40/1964", date(1964, 1, 1), date(2014, 1, 1), "IN_FORCE"),
        # genuinely future → correctly NOT_YET_EFFECTIVE, unchanged.
        _row(4, "999/2026", date(2026, 12, 1), None, "NOT_YET_EFFECTIVE"),
    ]
    sess = _FakeSession(rows)
    changes = await plan_restamp(sess, as_of=date(2026, 7, 2))
    by_ref = {c["ref"]: c for c in changes}
    assert set(by_ref) == {"455/1991", "40/1964"}
    assert by_ref["455/1991"]["old_status"] == "NOT_YET_EFFECTIVE"
    assert by_ref["455/1991"]["new_status"] == "IN_FORCE"
    assert by_ref["40/1964"]["new_status"] == "REPEALED"


@pytest.mark.asyncio
async def test_plan_restamp_is_as_of_pure():
    # The SAME row yields a DIFFERENT plan purely from as_of — proving the function reads no clock.
    rows = [_row(1, "455/1991", date(2026, 7, 1), None, "NOT_YET_EFFECTIVE")]
    before = await plan_restamp(_FakeSession([dict(r) for r in rows]), as_of=date(2026, 6, 30))
    after = await plan_restamp(_FakeSession([dict(r) for r in rows]), as_of=date(2026, 7, 2))
    assert before == []  # day before effect → already-correct → no change
    assert len(after) == 1 and after[0]["new_status"] == "IN_FORCE"


@pytest.mark.asyncio
async def test_apply_then_replan_is_idempotent():
    rows = [
        _row(1, "455/1991", date(2026, 7, 1), None, "NOT_YET_EFFECTIVE"),
        _row(3, "40/1964", date(1964, 1, 1), date(2014, 1, 1), "IN_FORCE"),
    ]
    sess = _FakeSession(rows)
    changes = await plan_restamp(sess, as_of=date(2026, 7, 2))
    n = await apply_restamp(sess, changes)
    assert n == 2
    # Re-planning at the same as_of after apply must yield ZERO further changes (idempotence).
    again = await plan_restamp(sess, as_of=date(2026, 7, 2))
    assert again == []
    # A second apply of an empty plan is a no-op.
    assert await apply_restamp(sess, again) == 0


@pytest.mark.asyncio
async def test_apply_issues_one_update_per_change():
    rows = [_row(1, "455/1991", date(2026, 7, 1), None, "NOT_YET_EFFECTIVE")]
    sess = _FakeSession(rows)
    changes = await plan_restamp(sess, as_of=date(2026, 7, 2))
    await apply_restamp(sess, changes)
    assert len(sess.executed) == 1
    sql, args = sess.executed[0]
    assert "UPDATE source_document SET temporal_status" in sql
    assert args == (1, "IN_FORCE")


def test_format_plan_groups_by_transition():
    changes = [
        {
            "ref": "455/1991",
            "old_status": "NOT_YET_EFFECTIVE",
            "new_status": "IN_FORCE",
            "valid_from": None,
            "valid_to": None,
            "id": 1,
        },
        {
            "ref": "187/2006",
            "old_status": "NOT_YET_EFFECTIVE",
            "new_status": "IN_FORCE",
            "valid_from": None,
            "valid_to": None,
            "id": 2,
        },
        {
            "ref": "40/1964",
            "old_status": "IN_FORCE",
            "new_status": "REPEALED",
            "valid_from": None,
            "valid_to": None,
            "id": 3,
        },
    ]
    out = _format_plan(changes, ref=date(2026, 7, 2))
    assert "NOT_YET_EFFECTIVE→IN_FORCE: 2 doc(s)" in out
    assert "IN_FORCE→REPEALED: 1 doc(s)" in out
    assert "TOTAL: 3 doc(s)" in out
    assert "455/1991" in out and "187/2006" in out


def test_format_plan_empty():
    assert "0 changes" in _format_plan([], ref=date(2026, 7, 2))
