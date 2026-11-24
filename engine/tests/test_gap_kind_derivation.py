"""VL-F-14 — unit tests for the deterministic gap_kind derivation (gap_kind_derive.py).

Each derivation RULE is pinned to its canonical 8-code output; the function is proven TOTAL (never raises on
any input) and proven to ALWAYS return a member of the canonical backlog set.
"""

from __future__ import annotations

import pytest

from pythia.reasoning.gap_kind import GAP_KIND_BACKLOG_CODES, GapKind
from pythia.reasoning.gap_kind_derive import GapSignals, derive_gap_kind


# ── per-rule units: each rule → its code ────────────────────────────────────────────────────────────────────
def test_uncovered_act_is_extraction_not_run():
    # rule 1 dominates everything else — a `where` slot on an uncovered act is still a coverage gap.
    assert derive_gap_kind(GapSignals(slot="where", act_covered=False)) == GapKind.EXTRACTION_NOT_RUN.value
    assert (
        derive_gap_kind(GapSignals(slot="steps", act_covered=False, is_delegating=True))
        == GapKind.EXTRACTION_NOT_RUN.value
    )


def test_delegating_without_implements_edge_is_decree_absent():
    # rule 2 — a provision that delegates authority (rel-11) with NO matching implements edge (rel-12).
    s = GapSignals(slot="obligations", act_covered=True, is_delegating=True, has_implements_edge=False)
    assert derive_gap_kind(s) == GapKind.DECREE_ABSENT.value
    # the decree IS in corpus (rel-12 present) ⇒ NOT decree_absent.
    s2 = GapSignals(slot="obligations", is_delegating=True, has_implements_edge=True)
    assert derive_gap_kind(s2) != GapKind.DECREE_ABSENT.value


def test_annual_instrument_stale():
    # rule 3
    assert (
        derive_gap_kind(GapSignals(slot="obligations", annual_instrument_stale=True))
        == GapKind.ANNUAL_INSTRUMENT_STALE.value
    )


def test_where_slot_is_authority_locator():
    # rule 4 — the two authority-pointer slots map to authority_locator.
    for slot in ("where", "authority", "WHERE"):
        assert derive_gap_kind(GapSignals(slot=slot)) == GapKind.AUTHORITY_LOCATOR.value


def test_steps_slot_is_form_portal():
    # rule 5 — the submission-channel / form slots map to form_portal.
    for slot in ("steps", "form", "channel", "procedure_step"):
        assert derive_gap_kind(GapSignals(slot=slot)) == GapKind.FORM_PORTAL.value


def test_soft_law_only_topic_is_soft_law_absent():
    # rule 6 — a non-where/steps slot whose topic is soft-law-only.
    assert derive_gap_kind(GapSignals(slot="obligations", soft_law_only=True)) == GapKind.SOFT_LAW_ABSENT.value


def test_text_probe_empty_is_no_text_support():
    # rule 8 — the act WAS covered, no fact row, and a live text probe also found nothing.
    assert derive_gap_kind(GapSignals(slot="obligations", text_probe_empty=True)) == GapKind.NO_TEXT_SUPPORT.value


def test_default_is_no_fact_row():
    # rule "else" — covered act, ordinary slot, no other signal ⇒ the benign no_fact_row fallback.
    assert derive_gap_kind(GapSignals(slot="obligations")) == GapKind.NO_FACT_ROW.value
    assert derive_gap_kind(GapSignals()) == GapKind.NO_FACT_ROW.value  # empty signals
    assert derive_gap_kind(GapSignals(slot="applies")) == GapKind.NO_FACT_ROW.value


# ── priority ordering (first match wins) ────────────────────────────────────────────────────────────────────
def test_coverage_beats_decree_and_slot():
    # rule 1 > rule 2 > rule 4: an uncovered act with a delegation flag AND a where slot ⇒ coverage wins.
    s = GapSignals(slot="where", act_covered=False, is_delegating=True, annual_instrument_stale=True)
    assert derive_gap_kind(s) == GapKind.EXTRACTION_NOT_RUN.value


def test_decree_beats_slot():
    # rule 2 > rule 4: a delegating `where` claim (covered act) ⇒ decree_absent, not authority_locator.
    s = GapSignals(slot="where", is_delegating=True, has_implements_edge=False)
    assert derive_gap_kind(s) == GapKind.DECREE_ABSENT.value


def test_slot_beats_soft_law():
    # rule 4/5 > rule 6: a where slot flagged soft_law_only still derives authority_locator (the slot is more
    # specific than the generic soft-law reason).
    s = GapSignals(slot="where", soft_law_only=True)
    assert derive_gap_kind(s) == GapKind.AUTHORITY_LOCATOR.value


# ── totality: never raises, always a canonical backlog code ─────────────────────────────────────────────────
@pytest.mark.parametrize("slot", ["", "where", "steps", "obligations", "applies", "risks", "UNKNOWN", "  ", "🙂"])
@pytest.mark.parametrize("act_covered", [True, False])
@pytest.mark.parametrize("is_delegating", [True, False])
def test_total_and_always_backlog_code(slot, act_covered, is_delegating):
    out = derive_gap_kind(GapSignals(slot=slot, act_covered=act_covered, is_delegating=is_delegating))
    assert out in GAP_KIND_BACKLOG_CODES


def test_derive_never_raises_on_weird_slot():
    # a slot with surprising whitespace/casing is normalised, never crashes.
    assert derive_gap_kind(GapSignals(slot="  Where  ")) == GapKind.AUTHORITY_LOCATOR.value
