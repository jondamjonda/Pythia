"""AQ-01 (engine half) — per-LLM-call token cost observability.

Proves the three load-bearing behaviours WITHOUT any live DB / model / paid SDK:
  (a) a paid adapter path (OpenAI + Anthropic, mocked) captures resp.usage into the per-request usage sink
      and bumps the engine telemetry counters (the /metrics surface);
  (b) the DEFAULT ollama path records NOTHING — no sink entries, no counter movement — the byte-identical
      guarantee (the answer's audit_trace["llm_usage"] key is OMITTED when the sink is empty);
  (c) record_llm_usage is a pure no-op when no request sink is set (a call outside an answer request),
      yet still bumps the aggregate counters, and the contextvar+list is shared by reference under a
      context-copying child task (the asyncio.gather safety the answer handler relies on).

The `openai`/`anthropic` packages are not installed; each test injects a minimal FAKE module into
sys.modules so the adapters' lazy import resolves to it — no real SDK or key is ever needed.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
import types

import pytest

from pythia import telemetry


# ── fakes: an OpenAI response carrying a usage object (the shape the adapter reads) ──────────────────
def _make_fake_openai(captured: dict, *, create_return=None):
    fake = types.ModuleType("openai")

    class _Completions:
        async def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return create_return

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _Models:
        async def list(self):
            return ["m"]

    class _FakeAsyncOpenAI:
        def __init__(self, *, api_key=None, timeout=None):
            self.chat = _Chat()
            self.models = _Models()

    fake.AsyncOpenAI = _FakeAsyncOpenAI
    return fake


def _openai_resp(content, *, prompt_tokens=0, completion_tokens=0, cached_tokens=0):
    """A chat-completions-like response with resp.usage.{prompt_tokens,completion_tokens} +
    resp.usage.prompt_tokens_details.cached_tokens (the cached-prompt count already inside prompt_tokens)."""
    msg = types.SimpleNamespace(content=content)
    choice = types.SimpleNamespace(message=msg)
    usage = types.SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=cached_tokens),
    )
    return types.SimpleNamespace(choices=[choice], usage=usage)


def _make_fake_anthropic(captured: dict, *, create_return=None):
    fake = types.ModuleType("anthropic")

    class _Messages:
        async def create(self, **kwargs):
            captured["create_kwargs"] = kwargs
            return create_return

    class _Models:
        async def list(self):
            return ["m"]

    class _FakeAsyncAnthropic:
        def __init__(self, *, api_key=None, timeout=None):
            self.messages = _Messages()
            self.models = _Models()

    fake.AsyncAnthropic = _FakeAsyncAnthropic
    return fake


def _anthropic_resp(text, *, input_tokens=0, output_tokens=0, cache_read=0):
    block = types.SimpleNamespace(type="text", text=text)
    usage = types.SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=cache_read
    )
    return types.SimpleNamespace(content=[block], usage=usage)


# ── (c) sink mechanism basics: no-op when unset, records when set, no-op when stage is None ───────────
def test_record_llm_usage_noop_when_no_sink() -> None:
    """No request sink set (usage_var default None) ⇒ record_llm_usage does not raise and appends nowhere,
    but STILL bumps the aggregate engine counters (so /metrics attributes standalone LLM spend too)."""
    # ensure no sink is bound in this context
    tok = telemetry.usage_var.set(None)
    try:
        before = telemetry.metrics()["llm_usage"]["calls_total"]
        telemetry.record_llm_usage("generate", "gpt-x", 10, 5, 2)  # must not raise
        after = telemetry.metrics()["llm_usage"]["calls_total"]
        assert after == before + 1
    finally:
        telemetry.usage_var.reset(tok)


def test_new_usage_sink_captures_and_shape() -> None:
    """new_usage_sink() installs a fresh list; record_llm_usage appends the exact wire-contract entry."""

    def _run() -> list[dict]:
        sink = telemetry.new_usage_sink()
        telemetry.record_llm_usage("decompose", "gpt-5.4-mini", 100, 20, 8)
        return sink

    # run in an isolated context so the sink does not leak into other tests
    sink = contextvars.copy_context().run(_run)
    assert sink == [
        {"stage": "decompose", "model": "gpt-5.4-mini", "tokens_input": 100, "tokens_output": 20, "tokens_cached": 8}
    ]


def test_sink_shared_by_reference_under_context_copy() -> None:
    """The contextvar+list semantics the answer handler relies on: a child task/context COPIED after the
    sink is set sees the SAME list object, so its appends are visible to the parent (the asyncio.gather
    safety — child tasks copy the context but share the list reference)."""

    async def _main() -> list[dict]:
        sink = telemetry.new_usage_sink()

        async def _child():
            telemetry.record_llm_usage("generate", "m", 1, 2, 0)  # appends into the shared list

        # asyncio.create_task copies the current context AFTER the sink is set → shares the reference
        await asyncio.create_task(_child())
        return sink

    sink = contextvars.copy_context().run(lambda: asyncio.run(_main()))
    assert len(sink) == 1 and sink[0]["stage"] == "generate"


# ── (a) paid adapter path captures usage into the sink + bumps counters ──────────────────────────────
def test_openai_adapter_records_usage_into_sink(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules,
        "openai",
        _make_fake_openai(
            captured, create_return=_openai_resp("hi", prompt_tokens=123, completion_tokens=45, cached_tokens=30)
        ),
    )
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    def _run() -> list[dict]:
        sink = telemetry.new_usage_sink()
        a = OpenAILLMAdapter(model="gpt-5.5")
        asyncio.run(a.text("ping", stage="generate"))
        return sink

    sink = contextvars.copy_context().run(_run)
    assert sink == [
        {"stage": "generate", "model": "gpt-5.5", "tokens_input": 123, "tokens_output": 45, "tokens_cached": 30}
    ]


def test_openai_adapter_no_stage_records_nothing(monkeypatch) -> None:
    """stage=None (a call outside the answer pipeline) ⇒ usage is NOT recorded even on the paid path."""
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules,
        "openai",
        _make_fake_openai(captured, create_return=_openai_resp("hi", prompt_tokens=10, completion_tokens=2)),
    )
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    def _run() -> list[dict]:
        sink = telemetry.new_usage_sink()
        asyncio.run(OpenAILLMAdapter(model="gpt-5.5").text("ping"))  # no stage
        return sink

    assert contextvars.copy_context().run(_run) == []


def test_openai_json_records_usage(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules,
        "openai",
        _make_fake_openai(
            captured, create_return=_openai_resp('{"ok": true}', prompt_tokens=7, completion_tokens=3, cached_tokens=0)
        ),
    )
    monkeypatch.setenv("PYTHIA_OPENAI_API_KEY", "sk-x")
    from pythia.llm_openai import OpenAILLMAdapter

    def _run() -> list[dict]:
        sink = telemetry.new_usage_sink()
        out = asyncio.run(
            OpenAILLMAdapter(model="gpt-5.4-mini").json("q", {"type": "object"}, stage="verify_faithfulness")
        )
        assert out == {"ok": True}
        return sink

    sink = contextvars.copy_context().run(_run)
    assert sink == [
        {
            "stage": "verify_faithfulness",
            "model": "gpt-5.4-mini",
            "tokens_input": 7,
            "tokens_output": 3,
            "tokens_cached": 0,
        }
    ]


def test_anthropic_adapter_records_usage(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        _make_fake_anthropic(
            captured, create_return=_anthropic_resp("hi", input_tokens=200, output_tokens=50, cache_read=12)
        ),
    )
    monkeypatch.setenv("PYTHIA_ANTHROPIC_API_KEY", "sk-ant")
    from pythia.llm_paid import PaidLLMAdapter

    def _run() -> list[dict]:
        sink = telemetry.new_usage_sink()
        asyncio.run(PaidLLMAdapter(model="claude-haiku-4-5").text("ping", stage="generate"))
        return sink

    sink = contextvars.copy_context().run(_run)
    assert sink == [
        {
            "stage": "generate",
            "model": "claude-haiku-4-5",
            "tokens_input": 200,
            "tokens_output": 50,
            "tokens_cached": 12,
        }
    ]


# ── (b) DEFAULT ollama path records nothing (byte-identical guarantee) ────────────────────────────────
def test_ollama_adapter_records_nothing(monkeypatch) -> None:
    """The default ollama adapter accepts the `stage` kwarg (signature-compat) but records NO usage: the
    sink stays empty, so answer() omits the audit_trace['llm_usage'] key → byte-identical default path."""
    from pythia.llm import OllamaLLMAdapter

    async def _fake_chat(self, prompt, *, fmt=None, system=None):
        return '{"x": 1}' if fmt is not None else "text-out"

    monkeypatch.setattr(OllamaLLMAdapter, "_chat", _fake_chat)

    def _run() -> list[dict]:
        sink = telemetry.new_usage_sink()
        a = OllamaLLMAdapter()
        asyncio.run(a.text("ping", stage="generate"))
        asyncio.run(a.json("q", {"type": "object"}, stage="decompose"))
        return sink

    assert contextvars.copy_context().run(_run) == []


# ── /metrics counters bump additively ────────────────────────────────────────────────────────────────
def test_metrics_counters_bump() -> None:
    """record_llm_usage bumps the aggregate + per-stage token counters exposed on GET /metrics."""
    m0 = telemetry.metrics()["llm_usage"]
    base_in = m0["tokens_input_total"]
    base_out = m0["tokens_output_total"]
    base_cached = m0["tokens_cached_total"]

    telemetry.usage_var.set(None)  # aggregate counters move regardless of a per-request sink
    telemetry.record_llm_usage("generate", "m", 300, 60, 40)

    m1 = telemetry.metrics()["llm_usage"]
    assert m1["tokens_input_total"] == base_in + 300
    assert m1["tokens_output_total"] == base_out + 60
    assert m1["tokens_cached_total"] == base_cached + 40
    assert m1["by_stage"]["generate"]["tokens_input"] >= 300
    assert m1["by_stage"]["generate"]["calls"] >= 1


def test_metrics_llm_usage_key_present_and_shaped() -> None:
    """The /metrics dict always carries the additive llm_usage block (0s on a fresh/ollama-only engine)."""
    m = telemetry.metrics()
    assert "llm_usage" in m
    for key in ("calls_total", "tokens_input_total", "tokens_output_total", "tokens_cached_total", "by_stage"):
        assert key in m["llm_usage"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
