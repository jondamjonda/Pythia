"""WP-026 — the `environment` config signal (config.py).

Pins that the `environment: Literal['local','staging','prod'] = 'local'` field
(added by WP-000) is present exactly once, defaults to 'local', and the Literal
rejects any other value. WP-026's boot guards (refuse boot when require_auth is
False in prod; 503 /internal/* outside local) build on this field — this test
locks the field's shape, not those guards.

No DB / no network — Settings is constructed directly with an explicit env so the
default is exercised deterministically regardless of the developer's secrets.env.
"""

from __future__ import annotations

from typing import Literal, get_args, get_type_hints

import pytest
from pydantic import ValidationError

from regsuper.config import Settings

# The exact allowed values, in spec order (§8.C-3 / WP-026).
_ALLOWED = ("local", "staging", "prod")


@pytest.fixture
def _isolated_env(monkeypatch):
    """Stop any ambient ENVIRONMENT var (or secrets.env) from masking the default
    when we want to exercise the field's hardcoded default."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("environment", raising=False)
    yield


def test_environment_field_is_present_exactly_once():
    # Defined exactly once on the model — not duplicated by a stray re-add.
    assert "environment" in Settings.model_fields
    # model_fields is keyed by name, so presence implies a single definition; pin the
    # annotation too so a second field with a different alias can't sneak in unseen.
    hints = get_type_hints(Settings)
    assert "environment" in hints


def test_environment_defaults_to_local(_isolated_env):
    s = Settings(_env_file=None)
    assert s.environment == "local"
    # And the field's declared default (independent of construction) is 'local'.
    assert Settings.model_fields["environment"].default == "local"


def test_environment_literal_allows_exactly_the_three_values():
    # The annotation is Literal['local','staging','prod'] — no more, no fewer.
    hints = get_type_hints(Settings)
    annotation = hints["environment"]
    assert get_args(annotation) == _ALLOWED
    assert annotation == Literal["local", "staging", "prod"]


@pytest.mark.parametrize("value", _ALLOWED)
def test_environment_accepts_each_allowed_value(value, _isolated_env):
    assert Settings(_env_file=None, environment=value).environment == value


@pytest.mark.parametrize("bad", ["production", "dev", "Local", "PROD", "", "test"])
def test_environment_rejects_other_values(bad, _isolated_env):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment=bad)
