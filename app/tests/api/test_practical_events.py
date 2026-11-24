"""VL-F-14 (WS6 step 57, app half) — the `practical.*` gap-backlog telemetry.

The two `practical.*` events route to the polarius control-room module, render human COUNTS-ONLY copy (not
the raw code), and are registered under the `practical.` prefix. The gap-backlog rollup is a stable ranked
count over the canonical 8-code vocabulary, and the emit path is COUNTS-ONLY (no query / claim text — WP-029).
"""

from __future__ import annotations

from regsuper.api import answer as answer_api
from regsuper.observability import events

_CODES = ("practical.gap", "practical.gap_backlog")


# ── prefix registration + routing ───────────────────────────────────────────────────────────────────────────
def test_practical_prefix_routes_to_polarius():
    for code in _CODES:
        assert events._module_for_code(code) == "polarius"


def test_practical_templates_registered():
    for code in _CODES:
        assert code in events.FRIENDLY_TEMPLATES


def test_practical_prefix_in_registry():
    # the ("practical.", "polarius") prefix is explicitly present in the module-prefix registry.
    assert ("practical.", "polarius") in events._MODULE_PREFIXES


# ── friendly copy renders COUNTS, not the raw code ──────────────────────────────────────────────────────────
def test_practical_gap_renders_count():
    out = events._render("practical.gap", {"n_gaps": 3})
    assert out != "practical.gap" and "3" in out


def test_practical_gap_backlog_renders_counts():
    out = events._render(
        "practical.gap_backlog", {"n_gaps": 4, "top_gap_kind": "decree_absent", "top_count": 2}
    )
    assert out != "practical.gap_backlog"
    assert "decree_absent" in out and "2" in out and "4" in out


# ── the gap_class → gap_kind mapper lands only canonical codes ──────────────────────────────────────────────
def test_gap_kind_of_maps_registry_classes_to_canonical():
    assert answer_api._gap_kind_of({"gap_class": "authority_locator"}) == "authority_locator"
    assert answer_api._gap_kind_of({"gap_class": "form_portal"}) == "form_portal"
    assert answer_api._gap_kind_of({"gap_class": "soft_law_guidance"}) == "soft_law_absent"
    assert answer_api._gap_kind_of({"gap_class": "regional"}) == "soft_law_absent"
    # a demote-path gap that already carries an explicit canonical gap_kind is used verbatim.
    assert (
        answer_api._gap_kind_of({"gap_kind": "decree_absent", "gap_class": "x"}) == "decree_absent"
    )
    # an unknown class collapses to the benign catch-all backlog code.
    assert answer_api._gap_kind_of({"gap_class": "???"}) == "soft_law_absent"


def test_gap_kind_of_always_returns_a_canonical_code():
    for gap in ({}, {"gap_class": ""}, {"gap_kind": "not_real"}, {"gap_class": "case_law"}):
        assert answer_api._gap_kind_of(gap) in events.GAP_KIND_BACKLOG_CODES


# ── the ranked rollup is stable (count desc, then canonical index) ──────────────────────────────────────────
def test_rollup_ranks_by_count_then_canonical_index():
    kinds = ["decree_absent"] * 2 + ["no_fact_row"] * 2 + ["form_portal"]
    out = answer_api._rollup_gap_backlog(iter(kinds))
    # decree_absent and no_fact_row tie at 2 → no_fact_row wins (canonical index 0 < decree_absent's 2).
    assert out[0] == ("no_fact_row", 2)
    assert out[1] == ("decree_absent", 2)
    assert out[2] == ("form_portal", 1)


def test_rollup_drops_non_canonical_codes():
    out = answer_api._rollup_gap_backlog(iter(["garbage", "", "no_fact_row"]))
    assert out == [("no_fact_row", 1)]


# ── the emit path is counts-only (WP-029) and best-effort ───────────────────────────────────────────────────
def test_emit_gap_telemetry_is_counts_only(monkeypatch):
    import asyncio

    captured: list[tuple[str, dict]] = []

    async def _fake_emit(code, **payload):
        captured.append((code, payload))
        return {"code": code, "payload": payload}

    monkeypatch.setattr(events, "emit", _fake_emit)

    block = answer_api.PracticalBlockOut(
        gaps=[
            {"gap_class": "authority_locator", "text": "…", "status": "DRAFT"},
            {"gap_class": "soft_law_guidance", "text": "…", "status": "DRAFT"},
            {"gap_kind": "decree_absent", "gap_class": "x", "text": "…", "status": "DRAFT"},
        ]
    )
    asyncio.run(answer_api._emit_gap_telemetry(block, org_id="org-1"))

    codes = [c for c, _ in captured]
    assert "practical.gap" in codes and "practical.gap_backlog" in codes
    # per-run count is the number of gaps.
    gap_payload = next(p for c, p in captured if c == "practical.gap")
    assert gap_payload["n_gaps"] == 3
    # NO query / claim / provision text in ANY payload — only counts + taxonomy labels.
    for _, payload in captured:
        for k, v in payload.items():
            assert k in {"org_id", "gap_class", "n_gaps", "top_gap_kind", "top_count", "module"}
            if isinstance(v, str):
                assert len(v) < 40  # taxonomy labels / ids are short; free text would be long


def test_emit_gap_telemetry_empty_block_is_silent(monkeypatch):
    import asyncio

    captured: list[str] = []

    async def _fake_emit(code, **payload):
        captured.append(code)
        return {}

    monkeypatch.setattr(events, "emit", _fake_emit)
    asyncio.run(answer_api._emit_gap_telemetry(answer_api.PracticalBlockOut(gaps=[]), org_id="o"))
    assert captured == []  # no gaps ⇒ zero rows (byte-identical)
