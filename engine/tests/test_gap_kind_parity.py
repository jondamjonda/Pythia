"""VL-F-14 (contract C11) — cross-repo gap_kind BACKLOG-SET parity (engine half).

The canonical 8-code backlog set (gap_kind.GAP_KIND_BACKLOG_CODES) is the connector-queue vocabulary shared
with the app (Polarius mirrors the SAME 8 strings in tests/api/test_gap_kind_parity.py). If the engine adds,
removes, or renames a backlog code, THIS test fails — forcing the mirrored change on the app side before the
drift becomes a silent telemetry mismatch. Mirrors the test_iv_enum_parity.py pattern.
"""

from __future__ import annotations

from pythia.reasoning.gap_kind import GAP_KIND_BACKLOG_CODES, GapKind, is_backlog_code

# ── The canonical 8-code backlog set (KEEP IN LOCKSTEP with Polarius/tests/api/test_gap_kind_parity.py) ──────
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


def test_engine_backlog_codes_match_canonical():
    # exact, ORDER-SENSITIVE parity — the tuple order is part of the contract (it is the rollup tie-break key).
    assert GAP_KIND_BACKLOG_CODES == CANONICAL_GAP_KIND_BACKLOG


def test_backlog_set_is_exactly_eight():
    assert len(GAP_KIND_BACKLOG_CODES) == 8
    assert len(set(GAP_KIND_BACKLOG_CODES)) == 8  # no duplicates


def test_every_backlog_code_is_a_gapkind_member():
    values = {k.value for k in GapKind}
    assert set(GAP_KIND_BACKLOG_CODES) <= values


def test_is_backlog_code_recognises_exactly_the_set():
    for code in CANONICAL_GAP_KIND_BACKLOG:
        assert is_backlog_code(code)
    # a valid GapKind that is NOT a backlog code (structural / catch-all) is rejected.
    assert not is_backlog_code(GapKind.UNKNOWN.value)
    assert not is_backlog_code(GapKind.OUT_OF_SCOPE.value)
    assert not is_backlog_code("not_a_real_code")
