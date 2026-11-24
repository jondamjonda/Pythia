"""C1 (MICRO) — PURE tests for the old-vs-new provision diff (no DB).

Covers diff_text (insertion old→None pole, repeal new→None pole, a real edit) and deontic_delta (a changed
deadline/condition + a flipped modal). version_diff's DB fetch is exercised against a FAKE async conn so NO
live pythia row is touched (TODAY every label has ≤1 version, so the <2-rows None path is the live reality).
"""

from __future__ import annotations

from pythia.graph import version_diff as vd


# ───────────────────────────────────────────────────────────────────────────────── diff_text
def test_diff_text_insertion_old_none():
    """old_text None ⇒ a brand-new provision: is_insertion True, everything in 'added', never raises."""
    d = vd.diff_text(None, "§ 1\nThe controller must notify the authority within 72 hours.")
    assert d["is_insertion"] is True
    assert d["is_repeal"] is False
    assert any("72 hours" in ln for ln in d["added"])
    assert d["removed"] == []


def test_diff_text_repeal_new_none():
    """new_text None ⇒ a closed head with no successor (REPEAL): is_repeal True, everything in 'removed'."""
    d = vd.diff_text("§ 7\nThis obligation applies to all credit institutions.", None)
    assert d["is_repeal"] is True
    assert d["is_insertion"] is False
    assert any("credit institutions" in ln for ln in d["removed"])
    assert d["added"] == []


def test_diff_text_real_edit_changed_block():
    """Two real versions: an edited line surfaces as a 'changed' {old,new} block, neither None pole set."""
    old = "§ 3\nThe deadline is 30 days.\nFinal."
    new = "§ 3\nThe deadline is 15 days.\nFinal."
    d = vd.diff_text(old, new)
    assert d["is_insertion"] is False and d["is_repeal"] is False
    assert d["changed"], "an edited line must produce a changed block"
    block = d["changed"][0]
    assert "30 days" in block["old"] and "15 days" in block["new"]


def test_diff_text_both_none_never_raises():
    d = vd.diff_text(None, None)
    assert d["is_insertion"] is True and d["is_repeal"] is True
    assert d["added"] == [] and d["removed"] == [] and d["changed"] == []


# ───────────────────────────────────────────────────────────────────────────────── deontic_delta
def test_deontic_delta_deadline_condition_change():
    """A changed deontic_condition (the SME 'deadline changed' headline) is reported; unchanged fields are not."""
    old = {
        "deontic_modal": "musí",
        "deontic_actor": "správce",
        "deontic_action": "oznámit",
        "deontic_condition": "do 30 dnů",
        "section_kind": "operative",
    }
    new = {**old, "deontic_condition": "do 15 dnů"}
    delta = vd.deontic_delta(old, new)
    assert set(delta) == {"deontic_condition"}
    assert delta["deontic_condition"] == ("do 30 dnů", "do 15 dnů")


def test_deontic_delta_modal_flip():
    """A flipped modal (obligation → permission) is the most consequential change — surfaced."""
    old = {"deontic_modal": "musí", "section_kind": "operative"}
    new = {"deontic_modal": "může", "section_kind": "operative"}
    delta = vd.deontic_delta(old, new)
    assert delta["deontic_modal"] == ("musí", "může")
    assert "section_kind" not in delta


def test_deontic_delta_none_snapshots():
    """Both-None ⇒ no change; one-None ⇒ every populated field surfaces (insertion/repeal of deontic shape)."""
    assert vd.deontic_delta(None, None) == {}
    d = vd.deontic_delta(None, {"deontic_modal": "musí"})
    assert d["deontic_modal"] == (None, "musí")


# ───────────────────────────────────────────────────────── version_diff against a FAKE conn (no live DB)
class _FakeConn:
    """A minimal async conn: fetch returns a preset list, fetchrow a preset row. NO real DB."""

    def __init__(self, rows, provision_row=None):
        self._rows = rows
        self._provision_row = provision_row

    async def fetch(self, *_a, **_k):
        return self._rows

    async def fetchrow(self, *_a, **_k):
        return self._provision_row


async def test_version_diff_returns_none_with_fewer_than_two_versions():
    """The TODAY live reality: max(version_no)=1 everywhere ⇒ <2 rows ⇒ None ('no prior version')."""
    conn = _FakeConn(
        rows=[
            {
                "version_no": 1,
                "text": "only v1",
                "valid_from": None,
                "valid_to": None,
                "text_hash": "h",
                "text_format": "gfm",
                "is_head": True,
            }
        ]
    )
    assert await vd.version_diff(conn, "doc-uuid", "§ 1") is None
    # empty result is also None
    assert await vd.version_diff(_FakeConn(rows=[]), "doc-uuid", "§ 1") is None


async def test_version_diff_assembles_old_new_and_diffs():
    """With two versions (DESC: [newer, older]) the diff pairs old←older.text, new←newer.text."""
    rows = [
        {
            "version_no": 2,
            "text": "§ 3 deadline is 15 days",
            "valid_from": None,
            "valid_to": None,
            "text_hash": "h2",
            "text_format": "gfm",
            "is_head": True,
        },
        {
            "version_no": 1,
            "text": "§ 3 deadline is 30 days",
            "valid_from": None,
            "valid_to": None,
            "text_hash": "h1",
            "text_format": "gfm",
            "is_head": False,
        },
    ]
    prow = {
        "deontic_modal": "musí",
        "deontic_actor": "správce",
        "deontic_action": "oznámit",
        "deontic_condition": "do 15 dnů",
        "section_kind": "operative",
    }
    out = await vd.version_diff(_FakeConn(rows=rows, provision_row=prow), "doc-uuid", "§ 3")
    assert out is not None
    assert out["old_text"] == "§ 3 deadline is 30 days"
    assert out["new_text"] == "§ 3 deadline is 15 days"
    assert out["text_diff"]["changed"], "the 30→15 day edit must show as changed"
    # deontic_delta is populated from the live provision snapshot (new side)
    assert out["deontic_delta"]["deontic_condition"] == (None, "do 15 dnů")
