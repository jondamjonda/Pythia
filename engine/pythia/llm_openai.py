"""OpenAI LLM adapter: the same LLMPort surface as OllamaLLMAdapter and PaidLLMAdapter.

One of three providers behind `pythia.llm_factory.get_llm(tier)`; reasoning/answer.py talks to it
through json() / text() / available() without knowing which provider is in use. It is constructed
only when `synthesizer_provider == "openai"`, and the factory passes in the per-tier model
(openai_generate_model or openai_cheap_model).

Invariants (shared with PaidLLMAdapter):
  - Lazy import: the `openai` SDK is imported inside __init__, never at module scope, so importing
    this module or the factory adds no hard dependency; the package need not be installed unless
    the OpenAI provider is selected.
  - Secret-safe: the API key is never logged, put in an exception message or echoed. It lives in a
    private attribute and does not appear in the adapter's repr or str.
  - Request surface (AsyncOpenAI Chat Completions): text() calls chat.completions.create;
    json() does the same with response_format={"type": "json_object"} and fail-closed parsing.
"""

from __future__ import annotations

from pythia import telemetry
from pythia.config import get_settings


class OpenAILLMAdapter:
    """LLMPort over the OpenAI Chat Completions API.

    Constructed by llm_factory only when the OpenAI provider is selected; the `openai` import is
    lazy, so the default path does not need the package.
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
        self.model = model or s.openai_generate_model
        # The key comes from the explicit argument, else the config secret, and is never logged. A
        # missing key is a hard error here. The factory already falls back to Ollama in that case, so
        # this only fires when the adapter is constructed directly without a key: fail loudly.
        self._api_key = api_key if api_key is not None else s.openai_api_key
        if not self._api_key:
            raise RuntimeError("OpenAILLMAdapter requires an OpenAI API key (set PYTHIA_OPENAI_API_KEY)")
        self.temperature = s.llm_temperature if temperature is None else temperature
        # Only a temperature passed to this adapter is sent to the API; see _common_kwargs.
        self._pinned_temperature = temperature
        self.timeout = s.llm_timeout_s if timeout is None else timeout
        self.max_tokens = max_tokens
        # Lazy import: only constructing this adapter imports openai, not importing this module or
        # the factory.
        try:
            import openai  # noqa: F401  (proves the package is available and builds the client)
        except ImportError as e:  # pragma: no cover - exercised only when the dep is genuinely absent
            raise RuntimeError(
                "synthesizer_provider='openai' requires the 'openai' package — pip install openai"
            ) from e
        self._openai = openai
        self._client = openai.AsyncOpenAI(api_key=self._api_key, timeout=self.timeout)

    def _common_kwargs(self) -> dict:
        """Return the request kwargs shared by json() and text().

        GPT-5.x chat models require ``max_completion_tokens``; the legacy ``max_tokens`` is rejected
        with a 400. ``temperature`` is sent only when the caller pinned one on this adapter: GPT-5.x
        and o-series models accept only the default temperature, so by default it is left to the
        API (the global ``llm_temperature`` is tuned for the local model and is not forwarded).
        """
        kwargs = {"model": self.model, "max_completion_tokens": self.max_tokens}
        if self._pinned_temperature is not None:
            kwargs["temperature"] = self._pinned_temperature
        return kwargs

    def _record_usage(self, resp, stage: str | None) -> None:
        """Record resp.usage in the per-request usage sink and the engine counters.

        A no-op when `stage` is None (a call outside the answer pipeline) or usage is absent;
        record_llm_usage also does nothing when no request sink is set. The cached count comes from
        prompt_tokens_details.cached_tokens, which OpenAI already includes in prompt_tokens; the app
        subtracts it once when costing. Best-effort: unexpected response shapes are swallowed so
        telemetry never breaks a real answer.
        """
        if stage is None:
            return
        try:
            usage = getattr(resp, "usage", None)
            if usage is None:
                return
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) if details is not None else 0
            telemetry.record_llm_usage(
                stage,
                self.model,
                int(getattr(usage, "prompt_tokens", 0) or 0),
                int(getattr(usage, "completion_tokens", 0) or 0),
                int(cached or 0),
            )
        except Exception:  # pragma: no cover - telemetry must never break an answer
            pass

    @staticmethod
    def _first_text(resp) -> str:
        """Return the assistant message text from a chat-completions response.

        A response with no choices or None content (for example a refusal or a filtered completion)
        yields "", which the answer pipeline treats as a failed draft that verification refuses:
        fail-closed, never a crash. Mirrors PaidLLMAdapter._first_text.
        """
        choices = getattr(resp, "choices", None) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        return (getattr(message, "content", None) or "") if message is not None else ""

    async def json(self, prompt: str, schema: dict, *, tier: str = "workhorse", stage: str | None = None) -> dict:
        """Return a parsed JSON dict using Chat Completions JSON mode (LLMPort.json).

        `tier` and `schema` are accepted for interface compatibility: the factory fixes the model
        per adapter instance, and JSON mode guarantees valid JSON while the prompt carries the
        expected shape. `stage` labels the call site for token-usage recording; None skips it.
        """
        import json as _json

        resp = await self._client.chat.completions.create(
            **self._common_kwargs(),
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        self._record_usage(resp, stage)
        # Fail-closed parse, as in PaidLLMAdapter: an empty or unparseable body raises
        # JSONDecodeError, which the pipeline treats as a failed draft.
        return _json.loads(self._first_text(resp))

    async def text(
        self, prompt: str, *, system: str | None = None, tier: str = "workhorse", stage: str | None = None
    ) -> str:
        """Return a plain text completion (LLMPort.text).

        `system`, when given, is sent as a leading system message. `stage` labels the call site for
        token-usage recording; None skips it.
        """
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = await self._client.chat.completions.create(
            **self._common_kwargs(),
            messages=messages,
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
