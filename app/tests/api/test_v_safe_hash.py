"""WS5 step 48(d, app half) — V-SAFE reserved-advice pattern-hash parity (cross-repo drift guard).

The app's ``regsuper.analysis.compliance_guard._RESERVED_ADVICE_RE`` is the CANONICAL reserved-advice
("act-on-behalf") pattern. The engine's per-claim V-SAFE guard duplicates it VERBATIM as
``pythia.reasoning.v_safe._TIER1_PATTERN_SRC`` (engine ⊥ app — it must not import the app) and exposes
``TIER1_PATTERN_VERSION`` = sha256[:16] of that copy. This test pins the app's canonical pattern hash to the
SAME literal the engine pins in PythiaRAG/Build/tests/test_v_safe_parity.py — so a one-sided edit to EITHER
copy changes one hash and breaks THAT repo's CI, forcing a re-sync. This mirrors the cross-repo IV-ENUM parity
guard in tests/api/test_iv_enum_parity.py.

The literal was verified to equal the engine's ``TIER1_PATTERN_VERSION`` at the time of writing (the two
compiled patterns are byte-identical, which is the whole point of the lawful-lane parity). If a lawyer revises
the reserved-advice list at G14, BOTH this literal and the engine's must be updated together — the paired CI
failures are the intended re-sync signal.
"""

from __future__ import annotations

import hashlib

from regsuper.analysis.compliance_guard import _RESERVED_ADVICE_RE

# ── PINNED reserved-advice pattern hash — KEEP IN LOCKSTEP with the engine's TIER1_PATTERN_VERSION
# (PythiaRAG/Build/pythia/reasoning/v_safe.py) and its pin in PythiaRAG/Build/tests/test_v_safe_parity.py. ──
RESERVED_ADVICE_PATTERN_VERSION = "a4fb7a4a9b57a08a"


def test_reserved_advice_pattern_hash_is_pinned():
    """The app's canonical reserved-advice pattern hashes to the pinned literal — a drift in the CANONICAL
    pattern (this side) fails here; the engine's verbatim copy is pinned to the same literal on its side."""
    got = hashlib.sha256(_RESERVED_ADVICE_RE.pattern.encode("utf-8")).hexdigest()[:16]
    assert got == RESERVED_ADVICE_PATTERN_VERSION, (
        "the canonical reserved-advice pattern changed — re-sync the engine V-SAFE copy "
        "(pythia/reasoning/v_safe.py _TIER1_PATTERN_SRC) and update BOTH pinned literals."
    )
