"""Citation hallucination check: does the LLM's quoted text exist in the provision it cited?

L2 returns ``applicable_text_span`` and L3 returns each obligation's ``supporting_text``. Both
should be exact substrings of the source provision, but are not always: the LLM paraphrases,
changes case, swaps quote characters or invents quotes outright. Without a check such a finding
looks plausible to a lawyer scanning the obligation register, and the audit log records a wrong
claim.

Both sides are normalised (whitespace collapsed, outer quotes stripped, case-folded) and compared
as substrings. If that misses, a forgiving token check follows: at least 70% of the span's tokens
must appear within one span-length window of the source. Neither check calls an LLM.

Verdicts:

* ``"verified"``: substring match after normalisation.
* ``"fuzzy"``: token match; minor LLM drift.
* ``"not_found"``: the quote is not in the provision; the finding is suspect.
* ``"empty"``: the LLM gave no quote (only valid for a not-applicable verdict).
"""

from __future__ import annotations

import re
from typing import Literal

CitationVerdict = Literal["verified", "fuzzy", "not_found", "empty"]

_WS_RE = re.compile(r"\s+")
_OUTER_QUOTES_RE = re.compile(r'^["\'`“”‘’\xab\xbb‹›]+|["\'`“”‘’\xab\xbb‹›]+$')
_PUNCT_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)


def _normalise(s: str) -> str:
    """Strip outer quotes, collapse whitespace and case-fold.

    Unicode-aware, so Czech diacritics survive.
    """
    if not s:
        return ""
    s = _OUTER_QUOTES_RE.sub("", s.strip())
    s = _WS_RE.sub(" ", s)
    return s.casefold()


def _tokens(s: str) -> list[str]:
    """Tokenise case-insensitively, dropping punctuation.

    Diacritics are kept because they carry meaning in Czech provisions (compare 'plnění' with
    'plneni').
    """
    s = _PUNCT_RE.sub(" ", s)
    return [t for t in _WS_RE.split(s.casefold()) if t]


def _fuzzy_token_match(
    span_tokens: list[str], source_tokens: list[str], *, min_ratio: float = 0.7
) -> bool:
    """Return True if enough of the span's tokens appear in one window of the source.

    A window is a contiguous slice of the source as long as the span; at least ``min_ratio`` of
    its tokens (and at least two) must be span tokens, in any order. This forgives reordered
    clauses and minor LLM paraphrase while still requiring the actual content tokens.
    """
    if not span_tokens:
        return False
    if len(span_tokens) > len(source_tokens):
        return False
    target = max(2, int(len(span_tokens) * min_ratio))
    span_set = set(span_tokens)
    # Sliding window the length of the span over the source.
    win = len(span_tokens)
    best = 0
    for i in range(len(source_tokens) - win + 1):
        overlap = sum(1 for t in source_tokens[i : i + win] if t in span_set)
        if overlap > best:
            best = overlap
            if best >= target:
                return True
    return False


def verify_citation(*, span: str | None, provision_text: str) -> CitationVerdict:
    """Compare an LLM-asserted span against the provision text."""
    if not span or not span.strip():
        return "empty"
    if not provision_text:
        return "not_found"

    norm_span = _normalise(span)
    norm_text = _normalise(provision_text)
    if not norm_span:
        return "empty"

    # Tier 1: exact normalised substring. Cheaply catches about 85% of valid
    # quotes (whitespace and casing differences only).
    if norm_span in norm_text:
        return "verified"

    # Tier 2: token sliding window. Forgives minor LLM rephrasing.
    span_tokens = _tokens(span)
    source_tokens = _tokens(provision_text)
    if _fuzzy_token_match(span_tokens, source_tokens):
        return "fuzzy"

    return "not_found"


def review_state_for(verdict: CitationVerdict) -> str:
    """Map a verification verdict to the ``finding.review_state`` to store.

    The dashboard and the lawyer review queue filter on this column.
    """
    if verdict in ("verified", "fuzzy"):
        return "unreviewed"  # the default state; no lawyer has seen it yet
    if verdict == "empty":
        return "needs_quote"  # we accepted the verdict but no quote backed it
    return "unverified_citation"  # not_found: the finding is suspect
