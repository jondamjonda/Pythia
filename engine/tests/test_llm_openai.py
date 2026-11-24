"""Phase-C M2 — the OpenAI provider behind the synthesizer seam (OpenAILLMAdapter).

Sibling of tests/test_llm_factory.py's anthropic coverage. These tests prove the OpenAI adapter's
invariants WITHOUT any network / real model / live DB / the `openai` package actually installed:
  • json() / text() shapes work against a MOCKED AsyncOpenAI client (no real API call).
  • available() probes models.list() and degrades to False on any error.
  • SECRET-SAFE: the api key never appears in repr/str of the adapter.
  • the OpenAI adapter requires a key (loud RuntimeError on a direct keyless construction).
  • fail-closed json parse: an empty / malformed body raises (treated as a failed draft upstream).

The `openai` package is not installed in this env, so each test injects a minimal FAKE `openai` module into
sys.modules — the lazy import inside OpenAILLMAdapter resolves to it, so no real SDK or key is ever needed.
The lazy-import-of-`openai` guarantee (importing pythia.llm_factory must NOT import openai) is asserted in
tests/test_llm_factory.py alongside the same guarantee for anthropic.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest


# ── a minimal fake `openai` module: AsyncOpenAI with chat.completions.create + models.list ────────────
def _make_fake_openai(captured: dict, *, create_return=None, create_raises=None, list_raises=None):
    """Build a fake `openai` module whose AsyncOpenAI records the api_key/timeout and whose
    chat.completions.create / models.list are async and return/raise per the args."""
    fake = types.ModuleType("openai")

    class _Completions:
        async def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            if create_raises is not None:
                raise create_raises
            return create_return

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Models:
        async def list(self):
            captured["models_list_called"] = True
            if list_raises is not None:
                raise list_raises
            return ["model-a"]

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key=None, timeout=None):
            captured["api_key"] = api_key
            captured["timeout"] = timeout
            self.chat = _Chat()
            self.models = _Models()

    fake.AsyncOpenAI = _FakeAsyncOpenAI
    return fake


def _resp(content):
    """Shape a chat-completions-like response: resp.choices[0].message.content == content."""
    msg = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])


def _install(monkeypatch, fake) -> None:
    monkeypatch.setitem(sys.modules, "openai", fake)


# ── construction + secret-safety ──────────────────────────────────────────────────────────────────────
def test_openai_adapter_requires_key(monkeypatch) -> None:
    """Constructing the OpenAI adapter directly without any key is a loud RuntimeError (the factory guards
    this with the ollama fallback; a direct construction must not silently run keyless)."""
    _install(monkeypatch, _make_fake_openai({}))
    monkeypatch.delenv("PYTHIA_OPENAI_API_KEY", raising=False)
    from pythia.llm_openai import OpenAILLMAdapter

    with pytest.raises(RuntimeError):
        OpenAILLMAdapter(model="gpt-5.5")


def test_api_key_not_in_adapter_repr(monkeypatch) -> None:
    """SECRET-SAFE: the key never appears in the adapter's repr/str (so it can't leak into a log line that
    prints the object)."""
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-openai-SUPER-SECRET-123")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.4-mini")
    assert "sk-openai-SUPER-SECRET-123" not in repr(a)
    assert "sk-openai-SUPER-SECRET-123" not in str(a)
    # but the key WAS threaded to the client unchanged (just not exposed via repr)
    assert captured["api_key"] == "sk-openai-SUPER-SECRET-123"


def test_api_key_from_explicit_arg(monkeypatch) -> None:
    """Explicit api_key arg is honoured (factory passes it / config secret is the fallback)."""
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured))
    monkeypatch.delenv("PYTHIA_OPENAI_API_KEY", raising=False)
    from pythia.llm_openai import OpenAILLMAdapter

    OpenAILLMAdapter(model="gpt-5.5", api_key="sk-explicit")
    assert captured["api_key"] == "sk-explicit"


# ── text() ────────────────────────────────────────────────────────────────────────────────────────────
def test_text_returns_assistant_content(monkeypatch) -> None:
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=_resp("hello world")))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.5")
    out = asyncio.run(a.text("ping"))
    assert out == "hello world"
    kw = captured["create_kwargs"]
    assert kw["model"] == "gpt-5.5"
    assert kw["messages"] == [{"role": "user", "content": "ping"}]
    assert "response_format" not in kw  # text() is a plain completion


def test_gpt5_param_contract_max_completion_tokens_no_max_tokens_no_temperature(monkeypatch) -> None:
    """GPT-5.x chat surface: the create call MUST use max_completion_tokens (legacy max_tokens 400s) and
    MUST NOT pin temperature (GPT-5.x/o-series reject a non-default temperature). Regression for the live
    400 'Unsupported parameter: max_tokens' / temperature errors."""
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=_resp("ok")))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    asyncio.run(OpenAILLMAdapter(model="gpt-5.5").text("ping"))
    kw = captured["create_kwargs"]
    assert "max_completion_tokens" in kw  # the GPT-5.x param
    assert "max_tokens" not in kw  # the legacy param that 400s
    assert "temperature" not in kw  # omitted → API default (GPT-5.x compatible)


def test_pinned_temperature_is_forwarded(monkeypatch) -> None:
    """A temperature passed to the adapter itself (e.g. for a model that accepts one) reaches the API;
    the global llm_temperature, tuned for the local model, still does not."""
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=_resp("ok")))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    asyncio.run(OpenAILLMAdapter(model="gpt-4.1", temperature=0.3).text("ping"))
    assert captured["create_kwargs"]["temperature"] == 0.3


def test_text_with_system_prepends_system_message(monkeypatch) -> None:
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=_resp("ok")))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.5")
    asyncio.run(a.text("u", system="s"))
    assert captured["create_kwargs"]["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


def test_text_empty_choices_returns_empty_string(monkeypatch) -> None:
    """A response with no choices (refusal / filtered) → "" (fail-closed, not a crash)."""
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=types.SimpleNamespace(choices=[])))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.5")
    assert asyncio.run(a.text("ping")) == ""


# ── json() ────────────────────────────────────────────────────────────────────────────────────────────
def test_json_parses_and_sets_json_object_response_format(monkeypatch) -> None:
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=_resp('{"topic": "AIF", "ok": true}')))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.4-mini")
    schema = {"type": "object", "properties": {"topic": {"type": "string"}, "ok": {"type": "boolean"}}}
    out = asyncio.run(a.json("classify", schema))
    assert out == {"topic": "AIF", "ok": True}
    kw = captured["create_kwargs"]
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["model"] == "gpt-5.4-mini"


def test_json_fail_closed_on_malformed_body(monkeypatch) -> None:
    """Fail-closed: an empty / unparseable body raises (mirrors PaidLLMAdapter letting json.loads raise)."""
    import json as _json

    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, create_return=_resp("not json at all")))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.5")
    with pytest.raises(_json.JSONDecodeError):
        asyncio.run(a.json("classify", {"type": "object"}))


# ── available() ───────────────────────────────────────────────────────────────────────────────────────
def test_available_true_on_models_list_ok(monkeypatch) -> None:
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.5")
    assert asyncio.run(a.available()) is True
    assert captured.get("models_list_called") is True


def test_available_false_on_error(monkeypatch) -> None:
    """Any error from models.list() (auth/network) → False, so the pipeline degrades gracefully."""
    captured: dict = {}
    _install(monkeypatch, _make_fake_openai(captured, list_raises=RuntimeError("boom")))
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    a = OpenAILLMAdapter(model="gpt-5.5")
    assert asyncio.run(a.available()) is False
