"""Anthropic LLM adapter: the same LLMPort surface as OllamaLLMAdapter.

One of three providers behind `pythia.llm_factory.get_llm(tier)`; reasoning/answer.py talks to it
through json() / text() / available() without knowing which provider is in use. It is constructed
only when `synthesizer_provider == "anthropic"`, and the factory passes in the per-tier model
(generate_model or cheap_model).

Invariants:
  - Lazy import: the `anthropic` SDK is imported inside __init__, so importing this module or the
    factory adds no hard dependency; the package need not be installed unless this provider is used.
  - Secret-safe: the API key is never logged, put in an exception message or echoed.
  - Request surface for Opus 4.8 / Haiku 4.5: adaptive thinking only (no budget_tokens), no
    temperature / top_p / top_k on Opus, and no assistant prefill. JSON output is constrained with
    output_config.format (json_schema), the generally available structured-output path.
"""

from __future__ import annotations

import copy

from pythia import telemetry
from pythia.config import get_settings

# Model-id prefixes that require the adaptive-thinking surface (no temperature / top_p, no
# budget_tokens). The Opus family and Fable share it; a cheap model (Haiku) still accepts temperature.
# Matching by prefix keeps the right surface when generate_model changes.
_ADAPTIVE_ONLY_PREFIXES = ("claude-opus-4-", "claude-fable-", "claude-mythos-", "claude-opus-4-8")


def _is_adaptive_only(model: str) -> bool:
    """Return True if `model` accepts adaptive thinking only.

    Opus 4.6+, Fable and Mythos reject temperature / top_p and budget_tokens; Haiku and Sonnet still
    accept a temperature. Matching by id prefix picks the right request surface per model without a
    hard-coded allow-list of exact ids.
    """
    m = (model or "").strip()
    return any(m.startswith(p) for p in _ADAPTIVE_ONLY_PREFIXES)


def _strict_schema(schema: dict) -> dict:
    """Return a deep copy of `schema` with additionalProperties: false on every object node.

    Anthropic structured outputs require it, but the engine's hand-written schemas do not set it
    (Ollama does not need it). The caller's schema object is left untouched.
    """

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "additionalProperties" not in node:
                node["additionalProperties"] = False
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    out = copy.deepcopy(schema)
    _walk(out)
    return out


class PaidLLMAdapter:
    """LLMPort over the Anthropic Messages API.

    Constructed by llm_factory only when the Anthropic provider is selected; the `anthropic` import
    is lazy, so the default path does not need the package.
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        max_tokens: int = 4096,
    ):
        s = get_settings()
        self.model = model or s.generate_model
        # The key comes from the explicit argument, else the config secret, and is never logged. A
        # missing key is a hard error here. The factory already falls back to Ollama in that case, so
        # this only fires when the adapter is constructed directly without a key: fail loudly.
        self._api_key = api_key if api_key is not None else s.anthropic_api_key
        if not self._api_key:
            raise RuntimeError("PaidLLMAdapter requires an Anthropic API key (set PYTHIA_ANTHROPIC_API_KEY)")
        self.temperature = s.llm_temperature if temperature is None else temperature
        self.timeout = s.llm_timeout_s if timeout is None else timeout
        self.max_tokens = max_tokens
        self._adaptive_only = _is_adaptive_only(self.model)
        # Lazy import: only constructing this adapter imports anthropic, not importing this module or
        # the factory.
        try:
            import anthropic  # noqa: F401  (proves the package is available and builds the client)
        except ImportError as e:  # pragma: no cover - exercised only when the dep is genuinely absent
            raise RuntimeError(
                "synthesizer_provider='anthropic' requires the 'anthropic' package — pip install anthropic"
            ) from e
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=self._api_key, timeout=self.timeout)

    def _common_kwargs(self) -> dict:
        """Return the request kwargs shared by json() and text().

        Adaptive-only models (Opus 4.8, Fable) reject temperature; the cheap tier (Haiku, Sonnet) keeps
        the configured temperature for determinism.
        """
        kw: dict = {"model": self.model, "max_tokens": self.max_tokens}
        if not self._adaptive_only:
            kw["temperature"] = self.temperature
        return kw

    def _record_usage(self, message, stage: str | None) -> None:
        """Record the message usage in the per-request sink and the engine counters (best-effort).

        Anthropic reports usage.input_tokens / output_tokens, with cache reads in
        cache_read_input_tokens. Unlike OpenAI, cache reads are not included in input_tokens; they
        are still reported as the contract's tokens_cached, and the app's OpenAI-shaped pricing
        handles the subtraction (Anthropic-specific costing is not modelled yet). A no-op when
        `stage` is None or usage is absent.
        """
        if stage is None:
            return
        try:
            usage = getattr(message, "usage", None)
            if usage is None:
                return
            cached = getattr(usage, "cache_read_input_tokens", 0)
            telemetry.record_llm_usage(
                stage,
                self.model,
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
                int(cached or 0),
            )
        except Exception:  # pragma: no cover - telemetry must never break an answer
            pass

    @staticmethod
    def _first_text(message) -> str:
        """Return the concatenated text of a Messages response, skipping thinking and other blocks.

        A refusal (stop_reason="refusal") has empty content and yields "", which the answer pipeline
        treats as a failed draft that verification refuses: fail-closed, never a crash.
        """
        parts = []
        for block in getattr(message, "content", None) or []:
            if getattr(block, "type", None) == "text":
                parts.append(getattr(block, "text", "") or "")
        return "".join(parts)

    async def json(self, prompt: str, schema: dict, *, tier: str = "workhorse", stage: str | None = None) -> dict:
        """Return schema-constrained JSON via output_config.format, parsed into a dict (LLMPort.json).

        `tier` is accepted for interface compatibility; the factory fixes the model per adapter
        instance. `stage` labels the call site for token-usage recording; None skips it.
        """
        import json as _json

        resp = await self._client.messages.create(
            **self._common_kwargs(),
            output_config={"format": {"type": "json_schema", "schema": _strict_schema(schema)}},
            messages=[{"role": "user", "content": prompt}],
        )
        self._record_usage(resp, stage)
        return _json.loads(self._first_text(resp))

    async def text(
        self, prompt: str, *, system: str | None = None, tier: str = "workhorse", stage: str | None = None
    ) -> str:
        """Return a plain text completion (LLMPort.text).

        `system`, when given, is sent as the top-level system prompt. `stage` labels the call site
        for token-usage recording; None skips it.
        """
        kwargs = self._common_kwargs()
        if system:
            kwargs["system"] = system
        resp = await self._client.messages.create(
            **kwargs,
            messages=[{"role": "user", "content": prompt}],
        )
        self._record_usage(resp, stage)
        return self._first_text(resp)

    async def available(self) -> bool:
        """Return True if the API is reachable, using a cheap models.list() round trip.

        Any error (auth, network) returns False so the pipeline degrades gracefully instead of
        crashing.
        """

        try:
            await self._client.models.list()
            return True
        except Exception:
            return False
