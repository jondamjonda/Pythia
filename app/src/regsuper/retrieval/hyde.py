"""HyDE (Hypothetical Document Embeddings) query expansion.

Asks a small LLM (OpenAI gpt-4o-mini) to draft what a regulation provision answering the query
would look like. Its embedding lands in the same vector neighbourhood as the real answer in a
way the raw question never does, because questions and provisions use different vocabulary.
Every call goes through `tracked_chat_completion`, so cost and latency are recorded in
`api_call`.
"""

from __future__ import annotations

from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.retrieval.cache import get_cached, make_key, put_cached

HYDE_KIND = "hyde"
HYDE_MODEL = "gpt-4o-mini"

HYDE_PROMPT = """You are a Czech and EU regulatory expert. Given a question
about regulatory compliance, write a hypothetical regulation provision that
would directly answer the question. Your output must:
- Be 2-3 sentences
- Use formal legal vocabulary (shall, may, must, where applicable)
- Sound like an actual provision, not a paraphrase of the question
- Be in the language of the likely source regulation (Czech for CZ-only
  questions, English for EU-level questions)

Do not include disclaimers. Do not say "this is hypothetical".
Output only the provision text.

Question: {query}
Hypothetical provision:"""


async def hyde(
    query: str,
    *,
    jurisdiction: str = "",
    sector: str = "",
    use_cache: bool = True,
) -> str:
    """Return one hypothetical provision answering `query`.

    Cached for 12 hours under (query, jurisdiction, sector), so re-runs in the dashboard
    cost nothing. `jurisdiction` and `sector` are part of the cache key but not of the
    prompt: the model is not conditioned on them, and the caller applies jurisdiction
    filtering at the search step.
    """
    key = make_key(HYDE_KIND, query, jurisdiction=jurisdiction, sector=sector)
    if use_cache:
        cached = await get_cached(key)
        if isinstance(cached, str) and cached.strip():
            return cached

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    response = await tracked_chat_completion(
        client,
        source="retrieval.hyde",
        model=HYDE_MODEL,
        messages=[{"role": "user", "content": HYDE_PROMPT.format(query=query)}],
        max_tokens=300,
        temperature=0.2,
    )
    out = (response.choices[0].message.content or "").strip()
    if out:
        await put_cached(key, kind=HYDE_KIND, value=out)
    return out
