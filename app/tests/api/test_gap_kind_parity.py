"""VL-F-14 (contract C11) — cross-repo gap_kind BACKLOG-SET parity (app half).

The app's `practical.gap_backlog` telemetry and its gap_class→gap_kind mapper depend on the canonical 8-code
backlog vocabulary the ENGINE owns (PythiaRAG pythia.reasoning.gap_kind.GAP_KIND_BACKLOG_CODES). Those eight
strings are MIRRORED here as a canonical contract; the engine repo pins the SAME literals in
tests/test_gap_kind_parity.py. Drift on either side fails that side's CI — the same guard the intake contract
uses (test_iv_enum_parity.py) so a silent telemetry mismatch can never ship.
"""

from __future__ import annotations

from regsuper.observability import events

# ── The canonical 8-code backlog set (KEEP IN LOCKSTEP with PythiaRAG/Build/tests/test_gap_kind_parity.py) ──
CANONICAL_GAP_KIND_BACKLOG: tuple[str, ...] = (
    "no_fact_row",
    "no_text_support",
    "decree_absent",
    "soft_law_absent",
    "authority_locator",
    "form_portal",
    "annual_instrument_stale",
    "extraction_not_run",
)


def test_app_backlog_codes_match_contract():
    # exact, ORDER-SENSITIVE parity — the tuple order is the rollup tie-break key, shared with the engine.
    assert events.GAP_KIND_BACKLOG_CODES == CANONICAL_GAP_KIND_BACKLOG


def test_backlog_set_is_exactly_eight_unique():
    assert len(events.GAP_KIND_BACKLOG_CODES) == 8
    assert len(set(events.GAP_KIND_BACKLOG_CODES)) == 8
