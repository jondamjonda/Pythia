"""LLM tier selector: picks the LLMPort adapter for each class of LLM call.

The provider (local Ollama by default, or Anthropic / OpenAI) comes from `synthesizer_provider`.
Provider SDKs are imported lazily inside their adapters, so importing this module never pulls in
anthropic or openai and the default local path has no hard dependency on them.
"""

from __future__ import annotations

from pythia.config import get_settings
from pythia.llm import OllamaLLMAdapter
from pythia.ports import LLMPort

_TIERS = ("generate", "cheap")


def get_llm(tier: str) -> LLMPort:
    """Return the LLMPort adapter for `tier`, either "generate" or "cheap".

    "generate" is the answer-drafting call (the most capable model). "cheap" covers decomposition,
    the question gate and faithfulness checks (short outputs, temperature 0, JSON mode).

    With the default provider "ollama", both tiers get the same local OllamaLLMAdapter, configured
    from settings (model, temperature, seed, timeout, keep_alive). "anthropic" and "openai" pick a
    per-tier model: generate_model / cheap_model, or openai_generate_model / openai_cheap_model.

    Fail-safe: if a paid provider is selected but its API key is missing, this falls back to the
    local Ollama adapter instead of crashing; an answer from the local model beats a 500. Whoever
    switches synthesizer_provider must also set PYTHIA_ANTHROPIC_API_KEY or PYTHIA_OPENAI_API_KEY.

    Raises:
        ValueError: if `tier` is not a known tier.
    """
    if tier not in _TIERS:
        raise ValueError(f"unknown LLM tier {tier!r}; expected one of {_TIERS}")
    s = get_settings()
    if s.synthesizer_provider == "anthropic":
        # Paid path: per-tier model, falling back to the local adapter when no key is configured.
        if not s.anthropic_api_key:
            return OllamaLLMAdapter()
        from pythia.llm_paid import PaidLLMAdapter  # lazy: anthropic import only happens on the paid path

        model = s.generate_model if tier == "generate" else s.cheap_model
        return PaidLLMAdapter(model=model)
    if s.synthesizer_provider == "openai":
        # OpenAI path: mirrors the Anthropic branch, including the fallback when no key is set.
        if not s.openai_api_key:
            return OllamaLLMAdapter()
        from pythia.llm_openai import OpenAILLMAdapter  # lazy: openai import only happens on the openai path

        model = s.openai_generate_model if tier == "generate" else s.openai_cheap_model
        return OpenAILLMAdapter(model=model)
    # Default: one local model serves every tier.

    return OllamaLLMAdapter()
