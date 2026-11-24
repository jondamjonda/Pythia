"""Local LLM adapter over Ollama (implements LLMPort); needs no paid API key.

Talks to a local Ollama server (default qwen2.5:14b on 127.0.0.1:11434) over HTTP:
  - json() uses Ollama's structured-output mode: a JSON schema constrains the model to valid JSON.
  - text() is a plain completion (synthetic-question generation, drafting, and so on).
This powers the LLM-dependent stages: synthetic training data, query decomposition, answer
generation and verification. Retrieval itself stays LLM-free.

Smoke:  .venv/bin/python -m pythia.llm
"""

from __future__ import annotations

import asyncio
import json as _json

import httpx

from pythia.config import get_settings


class OllamaLLMAdapter:
    """LLMPort over a local Ollama server."""

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
        *,
        temperature: float | None = None,
        timeout: float | None = None,
        seed: int | None = None,
        keep_alive: str | None = None,
    ):
        s = get_settings()
        self.model = model or s.ollama_model
        self.base = (host or s.ollama_host).rstrip("/")
        self.temperature = s.llm_temperature if temperature is None else temperature
        # A fixed seed makes Ollama generation reproducible (same prompt, model and parameters give
        # the same tokens), so /v1/answer does not drift between runs (answer versus abstain, or a
        # varying number of citations). seed=None means the configured default.
        self.seed = s.llm_seed if seed is None else seed
        self.timeout = s.llm_timeout_s if timeout is None else timeout
        # How long Ollama keeps the (large, 14B) model resident after each call. This trades memory
        # against reload latency and never changes the generated tokens. The default
        # (PYTHIA_OLLAMA_KEEP_ALIVE, "30s") unloads the model soon after an answer instead of
        # lingering for Ollama's multi-minute default.
        self.keep_alive = s.ollama_keep_alive if keep_alive is None else keep_alive

    async def _chat(self, prompt: str, *, fmt: dict | None = None, system: str | None = None) -> str:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        # `keep_alive` is a top-level /api/chat field, not an `options` key: it governs how long the
        # model stays in memory, not generation. The produced tokens depend only on the model,
        # messages, temperature and seed.
        payload: dict = {
            "model": self.model,
            "messages": msgs,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "seed": self.seed},
        }
        if fmt is not None:
            payload["format"] = fmt  # JSON schema constrains the output (Ollama 0.5+)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(f"{self.base}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()["message"]["content"]

    async def json(self, prompt: str, schema: dict, *, tier: str = "workhorse", stage: str | None = None) -> dict:
        """Return schema-constrained JSON (LLMPort.json).

        `tier` is ignored because there is one local model. `stage` is accepted for signature
        compatibility with the paid adapters but ignored: Ollama reports no real token usage, so no
        llm_usage rows are recorded.
        """
        return _json.loads(await self._chat(prompt, fmt=schema))

    async def text(
        self, prompt: str, *, system: str | None = None, tier: str = "workhorse", stage: str | None = None
    ) -> str:
        """Return a plain text completion (LLMPort.text).

        `tier` and `stage` are accepted for signature compatibility with the paid adapters and
        ignored; Ollama reports no token usage, so nothing is recorded.
        """

        return await self._chat(prompt, system=system)

    async def available(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return (await client.get(f"{self.base}/api/version")).status_code == 200
        except Exception:
            return False


async def _smoke() -> None:
    llm = OllamaLLMAdapter()
    print("server available:", await llm.available(), "· model:", llm.model)
    print("\n[CZ text] ->")
    print(await llm.text("Vysvětli jednou větou, co je depozitář investičního fondu."))
    print("\n[EN json] ->")
    schema = {
        "type": "object",
        "properties": {"topic": {"type": "string"}, "is_legal": {"type": "boolean"}},
        "required": ["topic", "is_legal"],
    }
    print(
        await llm.json(
            "Classify: 'marketing of an AIF to retail investors'. "
            "Return the topic and whether it is a legal/regulatory matter.",
            schema,
        )
    )


if __name__ == "__main__":
    asyncio.run(_smoke())
