"""WS5 step 48(d, engine half) — V-SAFE pattern-set hash parity (VL-D-08 cross-repo drift guard).

The engine's V-SAFE reserved-advice Tier-1 pattern (`v_safe._TIER1_PATTERN_SRC`) is a VERBATIM copy of the
app's canonical `regsuper.analysis.compliance_guard._RESERVED_ADVICE_RE` (the engine must not import the app —
engine ⊥ app — so the pattern is duplicated, not shared). This test PINS the pattern-set hash to a LITERAL so
any drift in the engine copy fails THIS side's CI; the app repo pins the SAME literal in
Polarius/tests/api/test_v_safe_hash.py, so a one-sided edit to either copy breaks that side's build and forces
a re-sync. This mirrors the cross-repo IV-ENUM parity pattern in tests/test_iv_enum_parity.py.

The literal below is the sha256[:16] of the shared reserved-advice pattern source. It was verified to equal the
hash of the app's live `_RESERVED_ADVICE_RE.pattern` at the time of writing — the two compiled patterns are
byte-identical, which is the whole point of the lawful-lane parity. The temporal-honesty "asserts-current"
vocab is pinned too (a second V-SAFE surface the app has no mirror for — see note in the temporal test).
"""

from __future__ import annotations

import hashlib

# ── PINNED reserved-advice pattern hash — KEEP IN LOCKSTEP with Polarius/tests/api/test_v_safe_hash.py ──
# (verified 2026-07-07 to equal hashlib.sha256(compliance_guard._RESERVED_ADVICE_RE.pattern)[:16])
RESERVED_ADVICE_PATTERN_VERSION = "a4fb7a4a9b57a08a"

# ── PINNED asserts-current temporal-honesty vocab hash (engine-only; the app carries no temporal V-SAFE mirror,
# so this is pinned here as a drift guard on the engine copy alone — see docstring note in test_asserts_current).
ASSERTS_CURRENT_VOCAB_VERSION = "b4f610a424d8d098"


def test_tier1_pattern_version_is_pinned():
    """The exposed TIER1_PATTERN_VERSION equals the pinned literal (drift in the engine copy fails here)."""
    from pythia.reasoning.v_safe import TIER1_PATTERN_VERSION

    assert TIER1_PATTERN_VERSION == RESERVED_ADVICE_PATTERN_VERSION


def test_tier1_pattern_version_is_the_hash_of_the_source():
    """TIER1_PATTERN_VERSION is genuinely the sha256[:16] of the pattern SOURCE (not a stale hard-coded string
    that drifted from the compiled regex)."""
    from pythia.reasoning.v_safe import _TIER1_PATTERN_SRC, TIER1_PATTERN_VERSION

    assert TIER1_PATTERN_VERSION == hashlib.sha256(_TIER1_PATTERN_SRC.encode("utf-8")).hexdigest()[:16]


def test_asserts_current_vocab_is_pinned():
    """The temporal-honesty 'asserts-current' detection vocab is pinned. This is an ENGINE-ONLY V-SAFE surface:
    the app's reserved-advice mirror covers only the lawful-lane Tier-1 pattern, NOT the temporal check, so this
    hash has no app-side counterpart — it guards the engine copy against a silent broadening of what counts as a
    'this rule is current' assertion (which would change which superseded-cite claims trip temporal_inconsistent).
    """
    from pythia.reasoning.v_safe import _RE_ASSERTS_CURRENT

    got = hashlib.sha256(_RE_ASSERTS_CURRENT.pattern.encode("utf-8")).hexdigest()[:16]
    assert got == ASSERTS_CURRENT_VOCAB_VERSION
