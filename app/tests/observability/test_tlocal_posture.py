"""WS10 step 81 — coverage guard over the EXPECTED T-LOCAL activation manifest.

The committed `manifests/expected_tlocal_manifest.md` encodes the fully-resolved ON/OFF posture the local
error-surfacing runner (`run_aq_all.sh`, untracked by design) brings up for the T-LOCAL everything-ON smoke,
so step 83 can diff a BOOTED env-manifest against it.

This guard pins one thing: the expected-manifest must MENTION every in-scope AQ answer-surface flag that the
app `docs/FLAG_GATES.md` registry governs (read via `flag_registry.aq_flag_names()`, the same
source the registry drift/lockstep guards use). If a new AQ flag is later added to the registry WITHOUT being
given an expected T-LOCAL value in the manifest, this fails — the manifest can never silently fall behind the
registry. Pure / DB-free.
"""

from __future__ import annotations

from pathlib import Path

from regsuper.observability.flag_registry import aq_flag_names

# tests/observability/ → parents[2] is the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _REPO_ROOT / "manifests" / "expected_tlocal_manifest.md"


def test_expected_manifest_exists():
    assert _MANIFEST.exists(), (
        f"missing committed expected T-LOCAL manifest: {_MANIFEST}. "
        "Author it (WS10 step 81) so step 83 can diff a booted env-manifest against it."
    )


def test_manifest_covers_every_app_registry_aq_flag():
    """Every in-scope AQ flag in the app flip-gate registry MUST have an expected T-LOCAL value in the manifest.

    Derived from `aq_flag_names()` (the live registry set) so it cannot drift: add an AQ flag to the registry
    without giving it a manifest row and this fails.
    """
    text = _MANIFEST.read_text(encoding="utf-8")
    missing = sorted(flag for flag in aq_flag_names() if f"`{flag}`" not in text)
    assert missing == [], (
        "expected_tlocal_manifest.md omits app-registry AQ flag(s) — give each an ON/OFF T-LOCAL value: "
        f"{missing}"
    )
