"""Per-role LLM model selection.

``resolve_model(role)`` returns the model string for a ``ModelRole``: a per-role environment
override if set, otherwise a hardcoded default. The result is cached, so a run pins one model per
role. Swapping models (including moving to another provider) is therefore a configuration change,
not a code change. L2, L3 and the compliance guard resolve their models here.

Defaults, used when no override is set:

* ``APPLICABILITY``: ``"gpt-4o-mini"`` (L2).
* ``OBLIGATIONS``: ``"gpt-4o"`` (L3).
* ``GAP_ANALYSIS``: ``"gpt-4o"``, deliberately the same string as OBLIGATIONS.
* ``TAGGING``: ``"gpt-4.1-mini"`` (matches ingest/tagging.py's ``TAG_MODEL``).
* ``CHEAP``: ``"gpt-5.4-mini"``.

The CHEAP default must be a model the OpenAI API serves. Its consumer
(``compliance_guard._classify``) sends it through the OpenAI client, so an Anthropic id here would
make every Tier-2 compliance call fail with ``model_not_found`` and fail closed to needs_review.
``gpt-5.4-mini`` is priced in ``model_prices_default.json``; ``MODEL_CHEAP`` still overrides it. No
code path may send a non-OpenAI id through the OpenAI client by default.
"""

from __future__ import annotations

import os
from enum import Enum
from functools import cache
from typing import Final

# Internal tracking tag; not used at runtime.
_IMPL_WP: Final[str] = "A.4 / WP-020"


class ModelRole(str, Enum):
    """The LLM call sites that select a model by role.

    The ``str`` mixin makes a role serialise as its value in audit payloads and config.
    """

    APPLICABILITY = "applicability"  # L2 applicability classification
    OBLIGATIONS = "obligations"  # L3 obligation extraction
    GAP_ANALYSIS = "gap_analysis"  # an LLM-based L4; shares the OBLIGATIONS model
    TAGGING = "tagging"  # corpus tagging at ingest
    CHEAP = "cheap"  # cheap utility model (compliance guard Tier-2, etc.)


# The defaults ``resolve_model`` returns when nothing overrides. Never default a role
# to 'claude-sonnet-4-6' (a retired config value); a test checks that no default
# mentions sonnet. CHEAP must be servable by OpenAI: its consumer routes through the
# OpenAI client, so any other default would fail every call with a 404.
_ROLE_DEFAULTS: Final[dict[ModelRole, str]] = {
    ModelRole.APPLICABILITY: "gpt-4o-mini",
    ModelRole.OBLIGATIONS: "gpt-4o",
    ModelRole.GAP_ANALYSIS: "gpt-4o",
    ModelRole.TAGGING: "gpt-4.1-mini",
    ModelRole.CHEAP: "gpt-5.4-mini",
}

# The per-role environment override. When unset, the default above applies. (The
# old ``analysis_model`` / ``cheap_model`` config keys are retired.)
_ROLE_ENV_VAR: Final[dict[ModelRole, str]] = {
    ModelRole.APPLICABILITY: "MODEL_APPLICABILITY",
    ModelRole.OBLIGATIONS: "MODEL_OBLIGATION",
    ModelRole.GAP_ANALYSIS: "MODEL_GAP",
    ModelRole.TAGGING: "MODEL_TAGGING",
    ModelRole.CHEAP: "MODEL_CHEAP",
}


@cache
def resolve_model(role: ModelRole) -> str:
    """Resolve the model string for an analysis ``role``.

    Precedence: the per-role environment override (``MODEL_APPLICABILITY``,
    ``MODEL_OBLIGATION``, ``MODEL_GAP``, ``MODEL_TAGGING``, ``MODEL_CHEAP``), then
    ``_ROLE_DEFAULTS``.

    Resolved once and cached (``functools.cache``), so a process pins a single model
    per role: the environment is read on first resolution and not again. An empty or
    whitespace-only override counts as unset, matching config's convention of
    treating an empty env var as None.
    """
    env_name = _ROLE_ENV_VAR.get(role)
    if env_name is not None:
        override = os.environ.get(env_name)
        if override is not None and override.strip() != "":
            return override.strip()
    return _ROLE_DEFAULTS[role]
