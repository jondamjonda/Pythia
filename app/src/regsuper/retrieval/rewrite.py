"""Multi-query rewriting of the user's question into retrieval-friendly variants.

Produces three variants:
  - formal_legal:       legal-vocabulary form
  - with_authorities:   names likely regulators (CNB, ESMA, Consob, ...)
  - with_defined_terms: uses defined-term phrasing likely to appear verbatim
                        in source statutes

Each variant can seed its own HyDE passage and search legs, fused via RRF (the orchestrator
caps how many with `max_hyde_variants`). Uses OpenAI gpt-4o-mini with
`response_format=json_object`; if the output cannot be parsed, the input query is returned so
retrieval can still proceed.
"""

from __future__ import annotations

import json
from typing import Any

from regsuper.analysis.llm_clients import get_openai
from regsuper.config import get_settings
from regsuper.observability.api_tracker import tracked_chat_completion
from regsuper.retrieval.cache import get_cached, make_key, put_cached

REWRITE_KIND = "rewrite"
REWRITE_MODEL = "gpt-4o-mini"

REWRITE_PROMPT = """You are a Czech and EU regulatory expert. Rewrite the
user's query into 3 search-friendly variants, each emphasising a different
aspect.

User query: {query}
Project context: jurisdiction={jurisdiction}, sector={sector}

Output JSON only with exactly these three string keys:
{{
  "formal_legal":      "<query in formal legal vocabulary>",
  "with_authorities":  "<query naming likely regulatory bodies, e.g. CNB, ESMA, Consob>",
  "with_defined_terms":"<query using likely statutory defined terms>"
}}"""

_VARIANT_KEYS = ("formal_legal", "with_authorities", "with_defined_terms")


async def rewrite(
    query: str,
    *,
    jurisdiction: str = "",
    sector: str = "",
    use_cache: bool = True,
) -> list[str]:
    """Return up to three rewritten variants for `query`.

    Always returns a list. If the LLM output is unusable, the original query is returned as
    the single fallback variant, so the orchestrator can still run some retrieval instead of
    failing the whole request.
    """
    key = make_key(REWRITE_KIND, query, jurisdiction=jurisdiction, sector=sector)
    if use_cache:
        cached = await get_cached(key)
        if isinstance(cached, list) and cached:
            return cached

    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")
    client = get_openai()

    response = await tracked_chat_completion(
        client,
        source="retrieval.rewrite",
        model=REWRITE_MODEL,
        messages=[
            {
                "role": "user",
                "content": REWRITE_PROMPT.format(
                    query=query, jurisdiction=jurisdiction, sector=sector
                ),
            }
        ],
        max_tokens=400,
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    body = (response.choices[0].message.content or "").strip()
    variants = _parse_variants(body)
    if not variants:
        variants = [query]

    await put_cached(key, kind=REWRITE_KIND, value=variants)
    return variants


def _parse_variants(body: str) -> list[str]:
    """Extract clean string variants from the model's JSON.

    Tolerates unexpected key order, missing keys, extra keys and a bare JSON list.
    """
    if not body:
        return []
    try:
        obj: Any = json.loads(body)
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    if isinstance(obj, dict):
        for k in _VARIANT_KEYS:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        # If the model returned extra string-valued keys, take them too.
        for k, v in obj.items():
            if k in _VARIANT_KEYS or not isinstance(v, str):
                continue
            if v.strip() and v.strip() not in out:
                out.append(v.strip())
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
    return out[:3]
