"""T-C1a — unit tests for the flip-safety preflight (inter-flag invariants).

All DB-free + pure: ``check_invariants`` / ``non_default_flags`` / ``run_preflight``
(no session) read only ``Settings``. The tests prove:
  * defaults ⇒ preflight passes silently (no raise, empty non-default map, the
    "all flags at safe default" INFO line);
  * each inconsistent flag combo is REFUSED loud (the right exception type);
  * a single flipped flag is reported on the non-default INFO line.

``run_preflight`` reads the cached ``get_settings()``, so we patch the cached
Settings object's attributes (the same approach test_session_scope_guc uses).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from regsuper.observability import flip_preflight
from regsuper.observability.flip_preflight import (
    FlipPreflightError,
    check_invariants,
    non_default_flags,
    run_preflight,
)
from regsuper.storage.database import RlsSafetyError


def _settings(**overrides):
    """A Settings-like object: every inspected flag at its safe default, with the
    given overrides applied. Using SimpleNamespace keeps the unit pure (no env /
    pydantic) — the preflight reads attributes via getattr only."""
    base = dict(flip_preflight._SAFE_DEFAULTS)
    base.update(overrides)
    return SimpleNamespace(**base)


# ===========================================================================
# Defaults — inert, passes silently.
# ===========================================================================
def test_defaults_pass_silently():
    s = _settings()
    # No raise.
    check_invariants(s)
    # Nothing differs from safe default.
    assert non_default_flags(s) == {}


async def test_run_preflight_at_defaults_logs_all_safe(monkeypatch):
    """At defaults: returns empty map + emits the 'all flags at safe default'
    INFO line, raises nothing, touches no DB (no session passed)."""
    monkeypatch.setattr(flip_preflight, "get_settings", lambda: _settings())

    logged: list[tuple[str, dict]] = []

    class _Logger:
        def info(self, event, **kw):
            logged.append((event, kw))

    monkeypatch.setattr(flip_preflight, "get_logger", lambda _n=None: _Logger())

    diffs = await run_preflight()
    assert diffs == {}
    assert logged == [("flip_preflight_all_flags_at_safe_default", {})]


# ===========================================================================
# RLS / tenancy invariant (LEDGER-M1) — reuses RlsSafetyError.
# ===========================================================================
def test_rls_enforced_without_tenancy_is_refused():
    with pytest.raises(RlsSafetyError):
        check_invariants(_settings(rls_enforced=True, tenancy_enabled=False))


def test_rls_enforced_with_tenancy_passes_config():
    # The pure-config pairing is satisfied; the DB-role half is a separate probe.
    check_invariants(_settings(rls_enforced=True, tenancy_enabled=True))


# ===========================================================================
# Cutover invariant (flags-gates-09).
# ===========================================================================
def test_cutover_approved_without_nexus_is_refused():
    with pytest.raises(FlipPreflightError) as ei:
        check_invariants(
            _settings(
                cutover_approved=True,
                law_source="regsuper",
                pythia_enabled=True,
                pythia_url="http://sidecar:8000",
            )
        )
    assert "law_source" in str(ei.value)


def test_cutover_approved_without_pythia_enabled_is_refused():
    with pytest.raises(FlipPreflightError) as ei:
        check_invariants(
            _settings(
                cutover_approved=True,
                law_source="nexus",
                pythia_enabled=False,
                pythia_url="http://sidecar:8000",
            )
        )
    assert "pythia_enabled" in str(ei.value)


def test_cutover_approved_without_pythia_url_is_refused():
    with pytest.raises(FlipPreflightError) as ei:
        check_invariants(
            _settings(
                cutover_approved=True,
                law_source="nexus",
                pythia_enabled=True,
                pythia_url=None,
            )
        )
    assert "pythia_url" in str(ei.value)


def test_cutover_approved_fully_configured_passes():
    check_invariants(
        _settings(
            cutover_approved=True,
            law_source="nexus",
            pythia_enabled=True,
            pythia_url="http://sidecar:8000",
        )
    )


# ===========================================================================
# Conduit invariant (WP-062 / LEDGER-M2).
# ===========================================================================
def test_conduit_enabled_is_refused_until_wp062():
    with pytest.raises(FlipPreflightError) as ei:
        check_invariants(_settings(conduit_enabled=True))
    assert "WP-062" in str(ei.value)


# ===========================================================================
# First-violation ordering — an RLS misconfig surfaces before a later one.
# ===========================================================================
def test_rls_invariant_checked_before_conduit():
    # Both rls (no tenancy) AND conduit are bad; RLS is checked first → RlsSafetyError.
    with pytest.raises(RlsSafetyError):
        check_invariants(_settings(rls_enforced=True, tenancy_enabled=False, conduit_enabled=True))


# ===========================================================================
# non_default_flags / the INFO line for a flipped flag.
# ===========================================================================
def test_non_default_flags_reports_single_flip():
    s = _settings(answer_endpoint_enabled=True)
    diffs = non_default_flags(s)
    assert diffs == {"answer_endpoint_enabled": True}


def test_non_default_flags_reports_multiple_and_string_values():
    s = _settings(profiles_enabled=True, l4_mode="llm")
    diffs = non_default_flags(s)
    assert diffs == {"profiles_enabled": True, "l4_mode": "llm"}


async def test_run_preflight_logs_non_default_flags(monkeypatch):
    """A safe-but-flipped flag (no invariant violated) is reported on the
    non-default INFO line; the run still passes (returns the map)."""
    monkeypatch.setattr(
        flip_preflight,
        "get_settings",
        lambda: _settings(audit_export_enabled=True),
    )

    logged: list[tuple[str, dict]] = []

    class _Logger:
        def info(self, event, **kw):
            logged.append((event, kw))

    monkeypatch.setattr(flip_preflight, "get_logger", lambda _n=None: _Logger())

    diffs = await run_preflight()
    assert diffs == {"audit_export_enabled": True}
    assert len(logged) == 1
    event, kw = logged[0]
    assert event == "flip_preflight_non_default_flags"
    assert kw["count"] == 1
    assert kw["flags"] == {"audit_export_enabled": True}


async def test_run_preflight_raises_on_bad_combo_before_logging(monkeypatch):
    """An invariant violation raises from run_preflight (fail-loud boot)."""
    monkeypatch.setattr(
        flip_preflight,
        "get_settings",
        lambda: _settings(conduit_enabled=True),
    )
    with pytest.raises(FlipPreflightError):
        await run_preflight()
