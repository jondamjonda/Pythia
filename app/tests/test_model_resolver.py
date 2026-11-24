"""WP-020 — per-role model resolver (analysis/model_resolver.py).

Pins the contract the seam must hold byte-identically to today's live hardcodes:
every default string is the exact value the L2/L3/L4 call-sites use now, a per-role
env override wins over the default, NOTHING ever defaults to the dead
'claude-sonnet-4-6' key, and each role default is defined exactly once.

No DB / no network — the resolver is pure (env + an in-module dict).
"""

from __future__ import annotations

from collections import Counter

import pytest

from regsuper.analysis.model_resolver import (
    _ROLE_DEFAULTS,
    ModelRole,
    resolve_model,
)

# The byte-identical live hardcodes (grep-confirmed against the LIVE constants):
#   l2_applicability.py  L2_MODEL  = "gpt-4o-mini"
#   l3_obligations.py    L3_MODEL  = "gpt-4o"
#   ingest/tagging.py    TAG_MODEL = "gpt-4.1-mini"
#   CHEAP default = "gpt-5.4-mini" — OpenAI-servable (WS0 step 4); the prior
#   claude-haiku default 404'd through the OpenAI client the CHEAP consumer uses.
_EXPECTED_DEFAULTS = {
    ModelRole.APPLICABILITY: "gpt-4o-mini",
    ModelRole.OBLIGATIONS: "gpt-4o",
    ModelRole.GAP_ANALYSIS: "gpt-4o",  # maps to the OBLIGATIONS string (decisions register)
    ModelRole.TAGGING: "gpt-4.1-mini",
    ModelRole.CHEAP: "gpt-5.4-mini",
}

# Per-role env override names the resolver reads (precedence layer 1).
_ENV_NAMES = {
    ModelRole.APPLICABILITY: "MODEL_APPLICABILITY",
    ModelRole.OBLIGATIONS: "MODEL_OBLIGATION",
    ModelRole.GAP_ANALYSIS: "MODEL_GAP",
    ModelRole.TAGGING: "MODEL_TAGGING",
    ModelRole.CHEAP: "MODEL_CHEAP",
}


@pytest.fixture(autouse=True)
def _clean_resolver(monkeypatch):
    """Each test starts from a clean slate: no model env overrides set, and the
    resolve_model lru_cache cleared so env reads aren't pinned across tests."""
    for env_name in _ENV_NAMES.values():
        monkeypatch.delenv(env_name, raising=False)
    resolve_model.cache_clear()
    yield
    resolve_model.cache_clear()


@pytest.mark.parametrize("role, expected", list(_EXPECTED_DEFAULTS.items()))
def test_default_is_exact_live_hardcode(role, expected):
    # With no env override, every role resolves to its byte-identical live default.
    assert resolve_model(role) == expected


def test_role_defaults_table_matches_live_hardcodes_exactly():
    # The in-module anchor table must equal the live hardcodes — no drift, no extras.
    assert _ROLE_DEFAULTS == _EXPECTED_DEFAULTS
    assert set(_ROLE_DEFAULTS) == set(ModelRole)


@pytest.mark.parametrize("role, env_name", list(_ENV_NAMES.items()))
def test_env_override_is_honoured(role, env_name, monkeypatch):
    # A per-role env var overrides the hardcoded default.
    monkeypatch.setenv(env_name, "some-override-model")
    resolve_model.cache_clear()
    assert resolve_model(role) == "some-override-model"


def test_empty_env_override_falls_through_to_default(monkeypatch):
    # An empty / whitespace override is treated as unset (config's empty-env→None convention).
    monkeypatch.setenv("MODEL_OBLIGATION", "   ")
    resolve_model.cache_clear()
    assert resolve_model(ModelRole.OBLIGATIONS) == "gpt-4o"


def test_env_override_does_not_leak_across_roles(monkeypatch):
    # Overriding one role must not change any other role's resolution.
    monkeypatch.setenv("MODEL_APPLICABILITY", "x-override")
    resolve_model.cache_clear()
    assert resolve_model(ModelRole.APPLICABILITY) == "x-override"
    assert resolve_model(ModelRole.OBLIGATIONS) == "gpt-4o"
    assert resolve_model(ModelRole.CHEAP) == "gpt-5.4-mini"


def test_no_role_ever_defaults_to_sonnet():
    # The retired config.py 'claude-sonnet-4-6' key is DEAD — it must appear NOWHERE
    # in the resolver's defaults (the cardinal "never default to sonnet" rule).
    for value in _ROLE_DEFAULTS.values():
        assert "sonnet" not in value.lower()
    for role in ModelRole:
        assert "sonnet" not in resolve_model(role).lower()


def test_gap_analysis_maps_to_obligations_string():
    # GAP_ANALYSIS deliberately shares the OBLIGATIONS model string (decisions register).
    assert resolve_model(ModelRole.GAP_ANALYSIS) == resolve_model(ModelRole.OBLIGATIONS)


def test_cheap_default_is_openai_servable():
    # WS0 step 4: the sole live CHEAP consumer (compliance_guard) routes through the
    # OpenAI client, so the CHEAP default MUST be an OpenAI-servable id — never a
    # claude/anthropic id (which 404'd model_not_found and fail-closed every request).
    cheap = resolve_model(ModelRole.CHEAP)
    assert "claude" not in cheap.lower()
    assert "anthropic" not in cheap.lower()
    assert cheap.startswith("gpt-")
    assert cheap == "gpt-5.4-mini"


def test_each_role_default_defined_exactly_once():
    # Single source of truth: one default per role, no duplicate ModelRole values.
    role_values = [r.value for r in ModelRole]
    assert len(role_values) == len(set(role_values))
    counts = Counter(_ROLE_DEFAULTS.keys())
    assert all(c == 1 for c in counts.values())
    assert len(_ROLE_DEFAULTS) == len(ModelRole)
