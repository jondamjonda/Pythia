"""VL-G-10 — the mechanized flip-gate registry guards (APP side).

Three independent guards over ``regsuper.observability.flag_registry``, all DB-free / pure:

  (b) LOCKSTEP — the committed ``docs/FLAG_GATES.md`` MUST equal the doc regenerated in memory
      from the live ``Settings``. A flag added/renamed without regenerating the doc fails here.

  (c) ``_SAFE_DEFAULTS`` COMPLETENESS — every AQ app flag the registry governs MUST have a row in
      ``flip_preflight._SAFE_DEFAULTS`` (else the boot non-default-flag line would silently omit a flipped AQ
      flag). WS8 VL-G-10 added the 6 previously-missing answer-surface rows; this test keeps the set complete.

  (f) DRIFT — the registry and the live Settings enumerate the SAME AQ answer-surface flag set (no ungoverned
      AQ flag, no stale registry row).
"""

from __future__ import annotations

from pathlib import Path

from regsuper.config import Settings
from regsuper.observability import flip_preflight
from regsuper.observability.flag_registry import (
    REGISTRY_DOC_NAME,
    aq_flag_names,
    registry_vs_settings_drift,
    render_markdown,
    settings_aq_flag_names,
)

# This test file is tests/observability/, so parents[2] is the repo root where the committed doc sits. (The
# generator resolves the SAME path via its own module location, parents[3] of src/regsuper/observability/.)
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ===========================================================================
# (b) LOCKSTEP — committed doc == freshly generated doc.
# ===========================================================================
def test_committed_registry_doc_matches_generated():
    """The committed `docs/FLAG_GATES.md` must be byte-identical to `render_markdown()`.

    Regenerate with `python -m regsuper.observability.flag_registry --write` after any AQ flag change."""
    doc = _REPO_ROOT / REGISTRY_DOC_NAME
    assert doc.exists(), f"missing committed registry doc: {doc}"
    committed = doc.read_text(encoding="utf-8")
    generated = render_markdown()
    assert committed == generated, (
        "The committed 'docs/FLAG_GATES.md' is STALE vs the live Settings. Regenerate it: "
        "`python -m regsuper.observability.flag_registry --write`."
    )


# ===========================================================================
# (f) DRIFT — registry and Settings enumerate the same AQ answer-surface flag set.
# ===========================================================================
def test_no_registry_settings_drift():
    drift = registry_vs_settings_drift()
    assert drift["missing_in_registry"] == [], (
        "AQ answer-surface flag(s) on Settings with NO flip-gate registry row (ungoverned): "
        f"{drift['missing_in_registry']}. Add a FlagGate row in flag_registry.py."
    )
    assert drift["stale_in_registry"] == [], (
        "Registry row(s) whose Settings field no longer exists (rename left behind): "
        f"{drift['stale_in_registry']}."
    )


def test_registry_and_settings_counts_agree():
    assert aq_flag_names() == settings_aq_flag_names()


def test_every_aq_flag_has_a_named_gate():
    from regsuper.observability.flag_registry import _REGISTRY

    for f in _REGISTRY:
        assert f.gate.strip(), f"flag {f.name!r} has no named gate"
        assert f.effect.strip(), f"flag {f.name!r} has no effect line"


def test_registry_rows_are_real_settings_fields():
    fields = set(Settings.model_fields)
    for name in aq_flag_names():
        assert name in fields, f"registry flag {name!r} is not a Settings field"


# ===========================================================================
# (c) _SAFE_DEFAULTS COMPLETENESS — every AQ app flag has a _SAFE_DEFAULTS entry.
# ===========================================================================
def test_every_aq_flag_has_a_safe_defaults_entry():
    """Every AQ app flag the registry governs MUST appear in flip_preflight._SAFE_DEFAULTS, so a flipped AQ
    flag is caught by the boot non-default-flag line."""
    missing = sorted(aq_flag_names() - set(flip_preflight._SAFE_DEFAULTS))
    assert not missing, (
        f"AQ app flag(s) missing from flip_preflight._SAFE_DEFAULTS: {missing}. Add a row (safe default) so "
        "the boot preflight surfaces the flag when it is flipped."
    )


def test_safe_defaults_values_match_live_settings_defaults():
    """The _SAFE_DEFAULTS value for each AQ flag must equal that flag's live Settings default (so a flip is
    detected against the RIGHT baseline, not a stale hand-typed one)."""
    s = Settings()
    mismatched: dict[str, tuple[object, object]] = {}
    for name in aq_flag_names():
        safe = flip_preflight._SAFE_DEFAULTS[name]
        live = getattr(s, name)
        if safe != live:
            mismatched[name] = (safe, live)
    assert not mismatched, (
        f"_SAFE_DEFAULTS value(s) diverge from the live Settings default: {mismatched}"
    )
