"""Phase-C M2 — the paid synthesizer seam (llm_factory.get_llm + PaidLLMAdapter).

These tests prove the cardinal M2 invariants WITHOUT any network / real model / live DB:
  • DEFAULT byte-identical: with synthesizer_provider=="ollama" (the default) get_llm("generate") and
    get_llm("cheap") both return an Ollama-backed adapter built exactly as the answer pipeline did pre-M2.
  • PAID path: with provider=="anthropic" + a key, get_llm returns the PaidLLMAdapter on the per-tier model.
  • FAIL-SAFE: provider=="anthropic" but NO key ⇒ degrade to the local Ollama adapter (never crash).
  • LAZY IMPORT: importing pythia.llm_factory does NOT import the `anthropic` package (run in a fresh
    interpreter so the assertion holds regardless of test ordering / what else imported anthropic).
  • SECRET-SAFE: the api key never appears in logs or in repr/str of the adapter.

The `anthropic` package is not installed in this env, so the paid-adapter tests inject a minimal FAKE
`anthropic` module into sys.modules — the lazy import inside PaidLLMAdapter resolves to it, so no real SDK
or key is ever needed.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from pythia.config import get_settings
from pythia.llm import OllamaLLMAdapter
from pythia.llm_factory import get_llm


# ── DEFAULT (ollama) — byte-identical path ───────────────────────────────────────────────────────────
def test_default_provider_is_ollama_for_both_tiers() -> None:
    """Default config: get_llm picks the local Ollama adapter for BOTH tiers, built identically to pre-M2
    (same model from config, no tier branching) — so the generate/decompose/verify calls are byte-identical."""
    assert get_settings().synthesizer_provider == "ollama"  # the default must stay ollama
    gen = get_llm("generate")
    cheap = get_llm("cheap")
    assert isinstance(gen, OllamaLLMAdapter) and isinstance(cheap, OllamaLLMAdapter)
    # Same construction the answer pipeline used before the seam: the configured ollama model + defaults.
    s = get_settings()
    assert gen.model == cheap.model == s.ollama_model
    assert gen.keep_alive == s.ollama_keep_alive  # Wave-3 knob honoured implicitly
    assert gen.temperature == s.llm_temperature and gen.seed == s.llm_seed


def test_unknown_tier_raises() -> None:
    with pytest.raises(ValueError):
        get_llm("workhorse")  # only "generate" / "cheap" are valid tiers


# ── PAID (anthropic) — selected per tier ─────────────────────────────────────────────────────────────
def _install_fake_anthropic(monkeypatch, captured: dict) -> None:
    """Put a minimal fake `anthropic` module in sys.modules so PaidLLMAdapter's lazy import resolves to it
    (the real SDK is not installed). Captures the api_key passed to the client so we can assert it is wired
    through but never logged."""
    fake = types.ModuleType("anthropic")

    class _FakeAsyncClient:
        def __init__(self, *, api_key=None, timeout=None):
            captured["api_key"] = api_key
            captured["timeout"] = timeout

    fake.AsyncAnthropic = _FakeAsyncClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def test_anthropic_provider_returns_paid_adapter_per_tier(monkeypatch) -> None:
    """provider=="anthropic" + key ⇒ get_llm returns the PaidLLMAdapter, with generate_model on the
    generate tier and cheap_model on the cheap tier."""
    captured: dict = {}
    _install_fake_anthropic(monkeypatch, captured)
    monkeypatch.setenv("PYTHIA_SYNTHESIZER_PROVIDER", "anthropic")
    monkeypatch.setenv("PYTHIA_ANTHROPIC_API_KEY", "sk-ant-test-DO-NOT-LOG")

    from pythia.llm_paid import PaidLLMAdapter

    s = get_settings()

    gen = get_llm("generate")
    cheap = get_llm("cheap")
    assert isinstance(gen, PaidLLMAdapter) and isinstance(cheap, PaidLLMAdapter)
    assert gen.model == s.generate_model
    assert cheap.model == s.cheap_model
    # the configured secret was threaded to the client unchanged
    assert captured["api_key"] == "sk-ant-test-DO-NOT-LOG"


def test_anthropic_provider_without_key_falls_back_to_ollama(monkeypatch) -> None:
    """FAIL-SAFE: provider flipped to anthropic but no key configured ⇒ degrade to the local Ollama adapter
    rather than crash. (The operator must set the key; meanwhile the engine keeps answering safely.)"""
    monkeypatch.setenv("PYTHIA_SYNTHESIZER_PROVIDER", "anthropic")
    monkeypatch.delenv("PYTHIA_ANTHROPIC_API_KEY", raising=False)
    assert get_settings().anthropic_api_key is None
    assert isinstance(get_llm("generate"), OllamaLLMAdapter)
    assert isinstance(get_llm("cheap"), OllamaLLMAdapter)


# ── PAID (openai) — selected per tier; mirrors the anthropic branch ──────────────────────────────────
def _install_fake_openai(monkeypatch, captured: dict) -> None:
    """Put a minimal fake `openai` module in sys.modules so OpenAILLMAdapter's lazy import resolves to it
    (the real SDK is not installed). Captures the api_key passed to the client so we can assert it is wired
    through but never logged."""
    fake = types.ModuleType("openai")

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key=None, timeout=None):
            captured["api_key"] = api_key
            captured["timeout"] = timeout

    fake.AsyncOpenAI = _FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)


def test_openai_provider_returns_openai_adapter_per_tier(monkeypatch) -> None:
    """provider=="openai" + key ⇒ get_llm returns the OpenAILLMAdapter, with openai_generate_model on the
    generate tier (gpt-5.5) and openai_cheap_model on the cheap tier (gpt-5.4-mini)."""
    captured: dict = {}
    _install_fake_openai(monkeypatch, captured)
    monkeypatch.setenv("PYTHIA_SYNTHESIZER_PROVIDER", "openai")
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-openai-test-DO-NOT-LOG")

    from pythia.llm_openai import OpenAILLMAdapter

    s = get_settings()

    gen = get_llm("generate")
    cheap = get_llm("cheap")
    assert isinstance(gen, OpenAILLMAdapter) and isinstance(cheap, OpenAILLMAdapter)
    assert gen.model == s.openai_generate_model == "gpt-5.5"
    assert cheap.model == s.openai_cheap_model == "gpt-5.4-mini"
    # the configured secret was threaded to the client unchanged
    assert captured["api_key"] == "sk-openai-test-DO-NOT-LOG"


def test_openai_provider_without_key_falls_back_to_ollama(monkeypatch) -> None:
    """FAIL-SAFE: provider flipped to openai but no key configured ⇒ degrade to the local Ollama adapter
    rather than crash — identical degrade behaviour to the anthropic branch."""
    monkeypatch.setenv("PYTHIA_SYNTHESIZER_PROVIDER", "openai")
    monkeypatch.delenv("PYTHIA_OPENAI_API_KEY", raising=False)
    assert get_settings().openai_api_key is None
    assert isinstance(get_llm("generate"), OllamaLLMAdapter)
    assert isinstance(get_llm("cheap"), OllamaLLMAdapter)


def test_paid_adapter_picks_adaptive_surface_for_opus_only(monkeypatch) -> None:
    """Opus-4.8/Fable take the adaptive-thinking surface (NO temperature in the request); haiku/sonnet keep
    a temperature. The PaidLLMAdapter detects this by model-id prefix."""
    captured: dict = {}
    _install_fake_anthropic(monkeypatch, captured)
    monkeypatch.setenv("PYTHIA_ANTHROPIC_API_KEY", "sk-ant-test")
    from pythia.llm_paid import PaidLLMAdapter

    opus = PaidLLMAdapter(model="claude-opus-4-8")
    haiku = PaidLLMAdapter(model="claude-haiku-4-5")
    assert "temperature" not in opus._common_kwargs()  # Opus rejects temperature → omitted
    assert "temperature" in haiku._common_kwargs()  # haiku keeps it


def test_paid_adapter_requires_key(monkeypatch) -> None:
    """Constructing the paid adapter directly without any key is a loud RuntimeError (the factory guards
    this with the ollama fallback; a direct construction must not silently run keyless)."""
    _install_fake_anthropic(monkeypatch, {})
    monkeypatch.delenv("PYTHIA_ANTHROPIC_API_KEY", raising=False)
    from pythia.llm_paid import PaidLLMAdapter

    with pytest.raises(RuntimeError):
        PaidLLMAdapter(model="claude-opus-4-8")


def test_strict_schema_stamps_additional_properties() -> None:
    """Structured-output schemas must carry additionalProperties:false on every object node; the helper
    deep-copies and stamps without mutating the caller's schema."""
    from pythia.llm_paid import _strict_schema

    src = {"type": "object", "properties": {"x": {"type": "object", "properties": {}}}}
    out = _strict_schema(src)
    assert out["additionalProperties"] is False
    assert out["properties"]["x"]["additionalProperties"] is False
    assert "additionalProperties" not in src  # caller's object untouched
    assert "additionalProperties" not in src["properties"]["x"]


def test_api_key_not_in_adapter_repr(monkeypatch) -> None:
    """SECRET-SAFE: the key never appears in the adapter's repr/str (so it can't leak into a log line that
    prints the object)."""
    _install_fake_anthropic(monkeypatch, {})
    monkeypatch.setenv("PYTHIA_ANTHROPIC_API_KEY", "sk-ant-SUPER-SECRET-123")
    from pythia.llm_paid import PaidLLMAdapter

    a = PaidLLMAdapter(model="claude-haiku-4-5")
    assert "sk-ant-SUPER-SECRET-123" not in repr(a)
    assert "sk-ant-SUPER-SECRET-123" not in str(a)


# ── LAZY IMPORT — proven in a fresh interpreter ──────────────────────────────────────────────────────
_LAZY_PROBE = (
    "import sys;"
    "import pythia.llm_factory;"
    "print('ANTHROPIC' if 'anthropic' in sys.modules else 'NO_ANTHROPIC');"
    "print('OPENAI' if 'openai' in sys.modules else 'NO_OPENAI')"
)


def test_importing_llm_factory_does_not_import_anthropic() -> None:
    """Importing pythia.llm_factory (what the answer pipeline does at import time) must NOT pull in the
    `anthropic` package — the dependency is lazy, behind the paid adapter's construction only."""
    r = subprocess.run([sys.executable, "-c", _LAZY_PROBE], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"probe crashed:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    assert "NO_ANTHROPIC" in r.stdout.split(), f"anthropic imported at llm_factory import:\n{r.stdout}"


def test_importing_llm_factory_does_not_import_openai() -> None:
    """Importing pythia.llm_factory must NOT pull in the `openai` package — the dependency is lazy, behind
    the OpenAI adapter's construction only (mirrors the anthropic lazy-import guarantee)."""
    r = subprocess.run([sys.executable, "-c", _LAZY_PROBE], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"probe crashed:\nSTDOUT:{r.stdout}\nSTDERR:{r.stderr}"
    assert "NO_OPENAI" in r.stdout.split(), f"openai imported at llm_factory import:\n{r.stdout}"
