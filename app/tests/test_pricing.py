"""Layered cost model — WP-027 (A4-02), observability/pricing.py.

`estimate_cost` reads data/model_prices.json layered OVER the committed
data/model_prices_default.json (TTL-cached). This suite pins the contract:

  * every seeded default key (per-token + per-call) returns a NON-ZERO cost;
  * an unknown / un-priced model returns 0.0, emits EXACTLY ONE
    `cost.model.unknown` event, and NEVER raises;
  * the operator override file wins over the committed default per key.

Everything is offline: we point pricing.get_settings() at a tmp data_dir whose
files we control, and capture pricing.emit() in-memory (no DB, no network).

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from regsuper.observability import pricing

# The full set of default keys seeded from the historical api_tracker dicts.
# These are the contract — every one must price to > 0 for a representative call.
_DEFAULT_TOKEN_KEYS = [
    "gpt-4o",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4o-mini",
    "gpt-4o-mini-2024-07-18",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "o1",
    "o1-mini",
    "text-embedding-3-large",
    "text-embedding-3-small",
]
_DEFAULT_PER_CALL_KEYS = [
    "rerank-v3.5",
    "rerank-multilingual-v3.0",
    "rerank-english-v3.0",
]


@contextmanager
def _book(tmp_path, *, default: dict, override: dict | None = None):
    """Write a default + (optional) override price file into a tmp data_dir, point
    pricing.get_settings() at it, capture emitted event codes, and reset the TTL
    cache on entry/exit so each test reads its own files. Yields the events list."""
    (tmp_path / pricing.DEFAULT_PRICES_FILENAME).write_text(json.dumps(default), encoding="utf-8")
    if override is not None:
        (tmp_path / pricing.PRICES_FILENAME).write_text(json.dumps(override), encoding="utf-8")

    events: list[tuple[str, dict]] = []

    async def fake_emit(code, *_a, **payload):
        events.append((code, payload))
        return None

    pricing._invalidate_cache()
    with (
        patch.object(pricing, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path)),
        patch.object(pricing, "emit", fake_emit),
    ):
        try:
            yield events
        finally:
            pricing._invalidate_cache()


def _real_default_book() -> dict:
    """The actually-committed default price book (the file we ship). Reading it
    here doubles as an assertion that the shipped seed exists and is valid JSON."""
    from pathlib import Path

    repo_data = Path(pricing.__file__).resolve().parents[3] / "data"
    return json.loads((repo_data / pricing.DEFAULT_PRICES_FILENAME).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- seeded keys
async def test_every_seeded_token_key_is_nonzero(tmp_path):
    # Use the REAL committed default book so this also asserts the shipped seed is complete.
    book = _real_default_book()
    with _book(tmp_path, default=book) as events:
        for model in _DEFAULT_TOKEN_KEYS:
            cost = await pricing.estimate_cost(
                model, kind="token", tokens_input=1000, tokens_output=1000
            )
            assert cost > 0.0, f"{model} priced to 0 — missing/zero default"
    assert events == [], "no seeded key should emit cost.model.unknown"


async def test_every_seeded_per_call_key_is_nonzero(tmp_path):
    book = _real_default_book()
    with _book(tmp_path, default=book) as events:
        for model in _DEFAULT_PER_CALL_KEYS:
            cost = await pricing.estimate_cost(model, kind="per_call")
            assert cost > 0.0, f"{model} per-call priced to 0"
    assert events == []


async def test_embedding_models_price_on_input_only(tmp_path):
    # Embedding models have output price 0 — cost comes purely from input tokens.
    book = _real_default_book()
    with _book(tmp_path, default=book):
        cost = await pricing.estimate_cost(
            "text-embedding-3-small", kind="token", tokens_input=1_000_000
        )
        assert cost == pytest.approx(0.02)  # $0.02 / 1M input tokens


async def test_cached_tokens_discounted(tmp_path):
    # cached tokens are already inside tokens_input; they bill at the (lower) cached rate.
    book = _real_default_book()
    with _book(tmp_path, default=book):
        full = await pricing.estimate_cost(
            "gpt-4o", kind="token", tokens_input=1_000_000, tokens_cached=0
        )
        cached = await pricing.estimate_cost(
            "gpt-4o", kind="token", tokens_input=1_000_000, tokens_cached=1_000_000
        )
    assert cached < full, "fully-cached input must cost less than fully-uncached"
    assert cached == pytest.approx(1.25)  # all 1M tokens at the $1.25 cached rate


# --------------------------------------------------------------------------- unknown model
async def test_unknown_token_model_returns_zero_and_emits_once(tmp_path):
    with _book(tmp_path, default={"token": {"gpt-4o": [2.5, 10.0, 1.25]}}) as events:
        cost = await pricing.estimate_cost(
            "totally-made-up-model-9000", kind="token", tokens_input=1000
        )
    assert cost == 0.0
    codes = [c for c, _ in events]
    assert codes == ["cost.model.unknown"], "exactly one unknown event"
    assert events[0][1]["model"] == "totally-made-up-model-9000"


async def test_unknown_per_call_model_returns_zero_and_emits_once(tmp_path):
    with _book(tmp_path, default={"per_call": {"rerank-v3.5": 0.002}}) as events:
        cost = await pricing.estimate_cost("rerank-made-up", kind="per_call")
    assert cost == 0.0
    assert [c for c, _ in events] == ["cost.model.unknown"]


async def test_unknown_model_never_raises_even_with_empty_book(tmp_path):
    # Empty book + missing override file → still must not raise.
    with _book(tmp_path, default={}) as events:
        cost = await pricing.estimate_cost("anything", kind="token", tokens_input=42)
    assert cost == 0.0
    assert [c for c, _ in events] == ["cost.model.unknown"]


async def test_corrupt_default_file_does_not_raise(tmp_path):
    # A malformed JSON default → treated as empty, unknown model → 0.0 + event (no raise).
    (tmp_path / pricing.DEFAULT_PRICES_FILENAME).write_text("{ not json", encoding="utf-8")
    events: list = []

    async def fake_emit(code, *_a, **payload):
        events.append(code)

    pricing._invalidate_cache()
    with (
        patch.object(pricing, "get_settings", lambda: SimpleNamespace(data_dir=tmp_path)),
        patch.object(pricing, "emit", fake_emit),
    ):
        cost = await pricing.estimate_cost("gpt-4o", kind="token", tokens_input=1000)
    pricing._invalidate_cache()
    assert cost == 0.0
    assert events == ["cost.model.unknown"]


# --------------------------------------------------------------------------- operator override
async def test_operator_override_wins_over_default(tmp_path):
    # Default prices gpt-4o at the standard rate; operator override doubles input price.
    default = {"token": {"gpt-4o": [2.5, 10.0, 1.25]}}
    override = {"token": {"gpt-4o": [5.0, 10.0, 1.25]}}
    with _book(tmp_path, default=default, override=override):
        cost = await pricing.estimate_cost("gpt-4o", kind="token", tokens_input=1_000_000)
    assert cost == pytest.approx(5.0), "operator file must override the committed default"


async def test_operator_override_extends_with_new_key(tmp_path):
    # A model absent from the default but present in the operator file must price from the override.
    default = {"token": {"gpt-4o": [2.5, 10.0, 1.25]}}
    override = {"token": {"brand-new-model": [9.0, 0.0, 0.0]}}
    with _book(tmp_path, default=default, override=override) as events:
        cost = await pricing.estimate_cost("brand-new-model", kind="token", tokens_input=1_000_000)
    assert cost == pytest.approx(9.0)
    assert events == [], "a model resolved from the override is NOT unknown"


async def test_default_survives_partial_override(tmp_path):
    # Override only touches per_call; the default token prices must still resolve.
    default = {"token": {"gpt-4o": [2.5, 10.0, 1.25]}, "per_call": {"rerank-v3.5": 0.002}}
    override = {"per_call": {"rerank-v3.5": 0.005}}
    with _book(tmp_path, default=default, override=override):
        token_cost = await pricing.estimate_cost("gpt-4o", kind="token", tokens_input=1_000_000)
        call_cost = await pricing.estimate_cost("rerank-v3.5", kind="per_call")
    assert token_cost == pytest.approx(2.5)  # untouched default still works
    assert call_cost == pytest.approx(0.005)  # override applied


# --------------------------------------------------------------------------- family heuristic
async def test_dated_variant_falls_back_to_family(tmp_path):
    # An un-pinned dated gpt-4o-mini variant should resolve via the family heuristic, not go unknown.
    with _book(tmp_path, default={"token": {"gpt-4o-mini": [0.15, 0.60, 0.075]}}) as events:
        cost = await pricing.estimate_cost(
            "gpt-4o-mini-2099-01-01", kind="token", tokens_input=1_000_000
        )
    assert cost == pytest.approx(0.15)
    assert events == [], "family-matched variant is not unknown"
