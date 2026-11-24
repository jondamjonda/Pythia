"""Mandatory-citation enforcement — does the LLM's quoted span actually exist in the cited provision?

This is the anti-hallucination gate (regsuper/analysis/citation_check.py). L2 (applicable_text_span)
and L3 (obligation supporting_text) both claim an EXACT quote from a provision. If the quote is
invented or drifted, a lawyer scanning the obligation register sees a plausible but WRONG claim, and
the audit log records it. `verify_citation()` returns one of four verdicts, and `review_state_for()`
routes a suspect finding into the review queue. Both are zero-LLM-cost pure functions → fully DB-free.

Verdict contract:
  verified  — exact substring after normalisation (whitespace/case/outer-quotes)
  fuzzy     — ≥70% of the span's tokens appear as a contiguous slice of the source
  not_found — the quote is not in the provision  → finding is SUSPECT
  empty     — no quote given (only acceptable for not-applicable)

review_state_for:
  verified|fuzzy → "unreviewed"          (lawyer hasn't seen it yet)
  empty          → "needs_quote"
  not_found      → "unverified_citation" (the suspect flag that MUST be surfaced)
"""

from __future__ import annotations

import pytest

from regsuper.analysis.citation_check import (
    review_state_for,
    verify_citation,
)

# A realistic Czech provision (ZISIF-style) — diacritics are meaningful and must survive.
PROVISION = (
    "Obhospodařovatel investičního fondu je povinen zavést a udržovat řídicí a kontrolní systém, "
    "který zahrnuje strategické a operativní řízení, systém řízení rizik a vnitřní kontrolu."
)


# --------------------------------------------------------------------------- verified (exact)
def test_exact_substring_is_verified():
    span = "je povinen zavést a udržovat řídicí a kontrolní systém"
    assert verify_citation(span=span, provision_text=PROVISION) == "verified"


def test_whitespace_and_case_differences_still_verified():
    # Collapsed whitespace + different case → normalisation makes it an exact match.
    span = "  JE  POVINEN   zavést a udržovat   řídicí a kontrolní   systém "
    assert verify_citation(span=span, provision_text=PROVISION) == "verified"


def test_outer_quotes_are_stripped_before_match():
    # Outer-quote stripping covers ASCII quotes and the curly “ ” pair (the regex char class in
    # _OUTER_QUOTES_RE). NB: it does NOT include the German low-quote „ (U+201E); see the next test.
    for q_open, q_close in (('"', '"'), ("“", "”"), ("'", "'")):
        span = f"{q_open}je povinen zavést a udržovat řídicí a kontrolní systém{q_close}"
        assert verify_citation(span=span, provision_text=PROVISION) == "verified", span


def test_low_quote_u201e_is_not_in_the_strip_set():
    # Documents a real edge of the implementation: „ (U+201E) is NOT stripped, so a span wrapped in it
    # falls through to the fuzzy token match rather than an exact substring. Pinned so a future change to
    # _OUTER_QUOTES_RE is a conscious decision, not a silent regression.
    span = "„je povinen zavést a udržovat řídicí a kontrolní systém“"
    assert verify_citation(span=span, provision_text=PROVISION) == "fuzzy"


def test_diacritics_are_load_bearing_not_stripped():
    # 'řídicí' with diacritics matches; the ASCII-folded 'ridici' must NOT verify, because the
    # checker deliberately keeps diacritics (plnění != plneni in Czech law).
    assert verify_citation(span="řídicí a kontrolní systém", provision_text=PROVISION) == "verified"
    assert verify_citation(span="ridici a kontrolni system", provision_text=PROVISION) != "verified"


# --------------------------------------------------------------------------- fuzzy (token-set window)
def test_minor_reordering_is_fuzzy_not_verified():
    # Same content tokens, reordered → not an exact substring, but ≥70% land in one window → fuzzy.
    span = "kontrolní a řídicí systém zavést a udržovat povinen"
    assert verify_citation(span=span, provision_text=PROVISION) == "fuzzy"


# --------------------------------------------------------------------------- not_found (suspect)
def test_invented_quote_is_not_found():
    span = "the entity must obtain prior written approval from the central bank within thirty days"
    assert verify_citation(span=span, provision_text=PROVISION) == "not_found"


def test_partially_overlapping_but_mostly_wrong_is_not_found():
    # Shares only a word or two with the source — below the 70% token floor → suspect.
    span = "systém povolení zahraniční pobočky devizový dohled sankce"
    assert verify_citation(span=span, provision_text=PROVISION) == "not_found"


def test_empty_provision_text_is_not_found():
    # A non-empty quote against an empty provision can't be grounded → suspect, never verified.
    assert verify_citation(span="anything at all", provision_text="") == "not_found"


# --------------------------------------------------------------------------- empty
def test_empty_span_is_empty():
    assert verify_citation(span=None, provision_text=PROVISION) == "empty"
    assert verify_citation(span="", provision_text=PROVISION) == "empty"
    assert verify_citation(span="   ", provision_text=PROVISION) == "empty"


def test_quotes_only_span_is_empty():
    # A span that is just quote characters normalises to "" → empty, not verified.
    assert verify_citation(span='""', provision_text=PROVISION) == "empty"


# --------------------------------------------------------------------------- review-state routing
@pytest.mark.parametrize(
    "verdict,expected",
    [
        ("verified", "unreviewed"),
        ("fuzzy", "unreviewed"),
        ("empty", "needs_quote"),
        ("not_found", "unverified_citation"),
    ],
)
def test_review_state_for_each_verdict(verdict, expected):
    assert review_state_for(verdict) == expected


def test_not_found_routes_to_the_suspect_state():
    """The trust-critical link: a hallucinated citation MUST land in 'unverified_citation' so the
    review queue surfaces it. If this routing breaks, suspect findings look clean."""
    verdict = verify_citation(
        span="completely fabricated obligation never present in the text",
        provision_text=PROVISION,
    )
    assert verdict == "not_found"
    assert review_state_for(verdict) == "unverified_citation"


def test_fuzzy_token_floor_boundary():
    """Directly exercise the 70% token floor: a span where well under 70% of tokens appear in any
    contiguous window must NOT be promoted to fuzzy (it would launder a hallucination as 'minor drift')."""
    source = "alpha beta gamma delta epsilon zeta eta theta"
    # 1 of 5 span tokens present in any window → 20% < 70% → not_found.
    assert verify_citation(span="alpha one two three four", provision_text=source) == "not_found"
    # All 3 span tokens present contiguously → verified (exact substring).
    assert verify_citation(span="gamma delta epsilon", provision_text=source) == "verified"
