"""Layered, hot-reloadable cost model for external API calls.

`estimate_cost(model, ...)` is the single source of truth for what an API call cost in USD. It
reads the operator override file `data/model_prices.json` layered over the committed defaults in
`data/model_prices_default.json`, so list-price changes apply without a code change or restart
(the merged price book is cached for a short TTL, after which the files are re-read).

Two price shapes, both keyed by model:

* "token": USD per million tokens, as [input, output, cached_input]. Used for OpenAI chat and
  embeddings. Cached tokens are already included in the prompt token count, so they are
  subtracted before the input price is applied (matching OpenAI's prompt-caching accounting).
* "per_call": a flat USD price per call. Used for Cohere rerank, which bills per search (one
  rerank call, up to 100 documents) rather than per token.

For an unknown model the function emits a `cost.model.unknown` event and returns 0.0. It never
raises: it sits on the hot path of every tracked API call, and a pricing problem must never
break the caller's work.

There are no Anthropic prices yet; they will be added together with an Anthropic client.
"""

from __future__ import annotations

import json
import time
from typing import Any

from regsuper.config import get_settings
from regsuper.observability.events import emit

# The committed default price book and the operator-managed override file (not committed).
# Both live under settings.data_dir, like limits.json.
DEFAULT_PRICES_FILENAME = "model_prices_default.json"
PRICES_FILENAME = "model_prices.json"

# Re-read the files at most this often: short enough that an operator edit is picked up within
# seconds, long enough that the hot path does not stat and parse them on every API call.
_CACHE_TTL_S = 30.0

# Module-level TTL cache of the merged price book.
_cache: dict[str, Any] | None = None
_cache_loaded_at: float = 0.0


def _load_json(path: Any) -> dict[str, Any]:
    """Read one price file; returns {} on any read or parse failure (never raises)."""
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _merge_section(base: dict[str, Any], override: dict[str, Any], section: str) -> dict[str, Any]:
    """Merge one price section, with the operator override winning per model.

    Documentation keys such as `_comment` and any section that is not a mapping are dropped, so
    only real model prices survive.
    """
    out: dict[str, Any] = {}
    for src in (base.get(section), override.get(section)):
        if not isinstance(src, dict):
            continue
        for model, price in src.items():
            if model.startswith("_"):  # documentation keys (e.g. "_comment")
                continue
            out[model] = price
    return out


def _build_book() -> dict[str, Any]:
    """Read both files and return the merged {"token": {...}, "per_call": {...}} book.

    Operator overrides (model_prices.json) take precedence over the committed defaults.
    """
    data_dir = get_settings().data_dir
    base = _load_json(data_dir / DEFAULT_PRICES_FILENAME)
    override = _load_json(data_dir / PRICES_FILENAME)
    return {
        "token": _merge_section(base, override, "token"),
        "per_call": _merge_section(base, override, "per_call"),
    }


def _book() -> dict[str, Any]:
    """Return the merged price book, rebuilding it when the TTL has lapsed."""
    global _cache, _cache_loaded_at
    now = time.monotonic()
    if _cache is None or (now - _cache_loaded_at) >= _CACHE_TTL_S:
        _cache = _build_book()
        _cache_loaded_at = now
    return _cache


def _invalidate_cache() -> None:
    """Drop the cached book (for tests, or to force a reload after an operator edit)."""
    global _cache, _cache_loaded_at
    _cache = None
    _cache_loaded_at = 0.0


def _token_price(model: str, token_book: dict[str, Any]) -> tuple[float, float, float] | None:
    """Resolve a model to its (input, output, cached) price per million tokens.

    A dated variant that is not listed falls back to its model family's price, which is a
    better estimate on the dashboard than 0.0.
    """
    price = token_book.get(model)
    if price is None:
        # Fall back to the model family for a dated variant that is not listed yet.
        if model.startswith("gpt-4o-mini"):
            price = token_book.get("gpt-4o-mini")
        elif model.startswith("gpt-4o"):
            price = token_book.get("gpt-4o")
        elif model.startswith("text-embedding-3-large"):
            price = token_book.get("text-embedding-3-large")
        elif model.startswith("text-embedding-3-small"):
            price = token_book.get("text-embedding-3-small")
    if price is None:
        return None
    try:
        p_in, p_out, p_cached = (float(price[0]), float(price[1]), float(price[2]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return p_in, p_out, p_cached


async def estimate_cost(
    model: str,
    *,
    kind: str = "token",
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_cached: int = 0,
) -> float:
    """Compute the USD cost of one API call from the layered price book.

    `kind` selects the price shape: "token" bills per million tokens (OpenAI chat and
    embeddings) using the token counts; "per_call" bills a flat price per call (Cohere rerank)
    and ignores the token counts.

    For an unknown or unpriced model, emits one `cost.model.unknown` event and returns 0.0.
    Never raises, since it runs on the hot path of every tracked call.
    """
    book = _book()

    if kind == "per_call":
        per_call = book.get("per_call", {})
        price = per_call.get(model)
        if price is None:
            await emit("cost.model.unknown", level="warn", model=model, kind=kind)
            return 0.0
        try:
            return float(price)
        except (TypeError, ValueError):
            await emit("cost.model.unknown", level="warn", model=model, kind=kind)
            return 0.0

    # kind == "token" (default)
    triple = _token_price(model, book.get("token", {}))
    if triple is None:
        await emit("cost.model.unknown", level="warn", model=model, kind=kind)
        return 0.0
    p_in, p_out, p_cached = triple
    # Cached tokens are already included in the input count; subtract them before applying the
    # (higher) uncached input price.
    uncached_in = max(0, tokens_input - tokens_cached)
    return (
        uncached_in * p_in / 1_000_000
        + tokens_cached * p_cached / 1_000_000
        + tokens_output * p_out / 1_000_000
    )
