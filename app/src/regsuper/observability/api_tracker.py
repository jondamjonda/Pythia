"""Wrap every external LLM, embedding and rerank API call and record it in the `api_call` table.

The wrappers are deliberately narrow and do not replace the provider clients: callers still
build their requests and hand the call to the tracker, which

1. honours the kill switch,
2. enforces the spend limits,
3. measures latency,
4. computes the cost from token usage, and
5. inserts an `api_call` row with the (redacted) request, the response and the cost.

Prices live in the layered price book (`observability/pricing.py`, reading
`data/model_prices_default.json` and the operator override `data/model_prices.json`), and every
cost is computed by `pricing.estimate_cost`. The table also stores raw token counts, so past
rows can be re-costed if prices change.

Caller text (chat messages, prompts, queries) and transport secrets are never stored verbatim:
see `_request_snapshot`.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import cohere
from openai import AsyncOpenAI

from regsuper.config import get_settings
from regsuper.observability import keys, killswitch, limits, pricing
from regsuper.observability.events import emit
from regsuper.storage.database import current_org_var, session_scope
from regsuper.storage.models import ApiCall


def _rls_org_fallback(org_id: UUID | None) -> UUID | None:
    """Default the api_call row's ``org_id`` to the active RLS org when the caller passed none.

    ``_persist_row`` opens its own ``session_scope()``, which sets ``app.current_org`` from
    ``current_org_var`` before the insert. Under enforced RLS, the ``p_api_call_tenant``
    policy's WITH CHECK requires ``org_id = current_setting('app.current_org')``, so a row with
    ``org_id=None`` would be rejected even though the session is correctly scoped to an org. (This
    happened to an LLM call on the answer path that passed its org to the audit log but not to
    the cost row.)

    Filling the org in here makes every tracked call's row match the RLS context it is written
    under, without relying on each caller. An explicit ``org_id`` always wins (metered analysis
    paths pass the project's org). With tenancy off, ``current_org_var`` is never set, so this
    returns None and the row has no org. Never raises: a malformed value falls back to None.
    """
    if org_id is not None:
        return org_id
    raw = current_org_var.get()
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


# The cost model lives in observability/pricing.py. These thin wrappers keep the older
# api_tracker call signatures working; both delegate to pricing.estimate_cost.
async def estimate_cost(
    model: str,
    *,
    tokens_input: int,
    tokens_output: int = 0,
    tokens_cached: int = 0,
) -> float:
    """Return the USD cost of one per-token API call (delegates to `pricing.estimate_cost`).

    An unknown model costs 0.0 and emits one `cost.model.unknown` event. Never raises.
    """
    return await pricing.estimate_cost(
        model,
        kind="token",
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cached=tokens_cached,
    )


async def estimate_cohere_cost(model: str) -> float:
    """Return the USD cost of one Cohere rerank call, priced per call.

    Delegates to `pricing.estimate_cost(kind="per_call")`. An unknown model costs 0.0 and emits
    one `cost.model.unknown` event. Never raises.
    """
    return await pricing.estimate_cost(model, kind="per_call")


class _KnownModels:
    """Read-only view of the known per-token models, standing in for the old `_PRICES` dict.

    Prices now live in the layered price book (pricing.py). External readers only need the set of
    known per-token models (e.g. the admin cost-config endpoint calls
    `api_tracker._PRICES.keys()`), so this proxies to the live token book and stays in sync with
    model_prices*.json without a code change.
    """

    def keys(self):
        return pricing._book().get("token", {}).keys()

    def __contains__(self, model: object) -> bool:
        return model in pricing._book().get("token", {})

    def __iter__(self):
        return iter(self.keys())


# Keeps the `_PRICES` access pattern working for existing readers.
_PRICES = _KnownModels()


# Keyword arguments carrying transport secrets, whose values are never stored in
# api_call.request_json, even with conduit_capture_query_text on, because replay never needs the
# caller's credentials. Matched case-insensitively against the exact key, and kept narrow so
# ordinary parameters (max_tokens, ...) are stored as usual.
_SENSITIVE_KWARG_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "bearer",
        "extra_headers",
        "default_headers",
        "headers",
        "secret",
        "password",
        "credentials",
        "access_token",
        "refresh_token",
    }
)


def _redact_sensitive_kwarg() -> dict[str, Any]:
    """Return the placeholder stored instead of a transport secret's value."""
    return {"_redacted": True, "reason": "sensitive_kwarg"}


def _request_snapshot(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe truncated copy of the request kwargs.

    Embedding `input` lists, which would add about a megabyte per call, are replaced by a
    summary.

    Caller-supplied free text (`messages`, `prompt`, `query`) is not stored verbatim by default:
    it is replaced by a marker describing its shape, with no content. Only when
    `settings.conduit_capture_query_text` is on is it kept verbatim for replay. Keeping it out
    of the api_call table by default also keeps it out of any export or log scan. Transport
    secrets are always redacted.
    """
    capture = get_settings().conduit_capture_query_text
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if k == "messages" and isinstance(v, list):
            out[k] = v if capture else _redact_messages(v)
        elif k == "prompt":
            out[k] = v if capture else _redact_text(v)
        elif k == "query":
            out[k] = v if capture else _redact_text(v)
        elif k == "input" and isinstance(v, list) and v and isinstance(v[0], str):
            out[k] = {
                "_truncated": True,
                "n_items": len(v),
                "sample": (v[0][:300] if v else "") if capture else "",
            }
        elif k.lower() in _SENSITIVE_KWARG_KEYS:
            out[k] = _redact_sensitive_kwarg()
        else:
            out[k] = v
    return out


def _redact_messages(messages: list[Any]) -> dict[str, Any]:
    """Replace a chat `messages` list with a marker that has no message content.

    Keeps the count and each message's role (cheap and not sensitive), so the row is still useful
    for debugging without storing the caller's text.
    """
    roles: list[Any] = []
    for m in messages:
        if isinstance(m, dict):
            roles.append(m.get("role", "?"))
        else:
            roles.append(getattr(m, "role", "?"))
    return {"_redacted": True, "n_messages": len(messages), "roles": roles}


def _redact_text(value: Any) -> dict[str, Any]:
    """Replace a free-text `prompt` or `query` with a marker giving only its type and length."""
    try:
        length = len(value) if isinstance(value, (str, list)) else None
    except TypeError:
        length = None
    return {"_redacted": True, "type": type(value).__name__, "length": length}


def _response_summary(
    *,
    endpoint: str,
    response: Any,
) -> tuple[str, dict[str, Any]]:
    """Return a short human-readable summary and a JSON-safe dict for replay."""
    try:
        d = response.model_dump() if hasattr(response, "model_dump") else dict(response)
    except Exception:
        d = {"_repr": repr(response)[:2000]}

    if endpoint == "chat.completions":
        try:
            msg = response.choices[0].message.content or ""
        except Exception:
            msg = ""
        summary = msg.strip()[:500]
    elif endpoint == "embeddings":
        try:
            n = len(response.data)
            dim = len(response.data[0].embedding) if response.data else 0
            summary = f"{n} embeddings, dim={dim}"
        except Exception:
            summary = "(embeddings)"
        # Drop the vectors themselves: they are huge and can be recomputed.
        if isinstance(d, dict) and "data" in d:
            d = dict(d)
            d["data"] = [
                {k: v for k, v in item.items() if k != "embedding"} for item in d["data"][:5]
            ]
            d["_data_truncated"] = True
    else:
        summary = ""
    return summary, d


async def _persist_row(
    *,
    provider: str,
    model: str,
    endpoint: str,
    source: str,
    project_id: UUID | None,
    org_id: UUID | None = None,
    analysis_run_id: int | None = None,
    tokens_input: int,
    tokens_output: int,
    tokens_cached: int,
    cost_usd: float,
    latency_ms: int,
    status: str,
    error: str | None,
    request_json: dict[str, Any],
    response_json: dict[str, Any],
    response_summary: str | None,
) -> UUID | None:
    """Insert one api_call row and return its id, or None if the database write fails.

    A database failure is reported as an event and swallowed, so a tracker problem never breaks
    the caller's work.

    `org_id` and `analysis_run_id` are optional and fill the nullable tenant and run columns.
    Tagging each row with its analysis run is what lets
    `entitlements.metering.finish_analysis_run` compute a run's cost as the sum of
    `api_call.cost_usd` for that run.
    """
    # Fill in the active RLS org when the caller passed none, so the insert passes the policy's
    # WITH CHECK (see _rls_org_fallback; stays None while tenancy is off).
    org_id = _rls_org_fallback(org_id)
    try:
        async with session_scope() as session:
            row = ApiCall(
                provider=provider,
                api_key_id=keys.active_key_id(provider),  # the budget line it counts against
                model=model,
                endpoint=endpoint,
                source=source,
                project_id=project_id,
                org_id=org_id,
                analysis_run_id=analysis_run_id,
                tokens_input=tokens_input,
                tokens_output=tokens_output,
                tokens_cached=tokens_cached,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                status=status,
                error=error,
                request_json=request_json,
                response_json=response_json,
                response_summary=response_summary,
            )
            session.add(row)
            await session.flush()
            return row.id
    except Exception as exc:
        await emit("api_tracker.persist_error", error=repr(exc))
        return None


async def tracked_chat_completion(
    client: AsyncOpenAI,
    *,
    source: str,
    project_id: UUID | None = None,
    org_id: UUID | None = None,
    analysis_run_id: int | None = None,
    **kwargs: Any,
) -> Any:
    """Call `client.chat.completions.create` and record its cost, latency and (redacted) I/O.

    The kill switch must be off and spend must be under the configured caps; otherwise
    `KillSwitchEngaged` or `SpendLimitExceeded` is raised before the call, so the caller can show
    a clear error. `org_id` and `analysis_run_id` are optional and are stored on the api_call row
    for per-run cost roll-up.
    """
    killswitch.assert_not_killed()
    await limits.enforce_spend_limit(provider="openai")

    model = kwargs.get("model", "unknown")
    t0 = time.perf_counter()
    error_msg: str | None = None
    response: Any = None
    status = "ok"

    try:
        response = await client.chat.completions.create(**kwargs)
    except Exception as exc:
        status = "error"
        error_msg = repr(exc)[:2000]
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await _persist_row(
            provider="openai",
            model=model,
            endpoint="chat.completions",
            source=source,
            project_id=project_id,
            org_id=org_id,
            analysis_run_id=analysis_run_id,
            tokens_input=0,
            tokens_output=0,
            tokens_cached=0,
            cost_usd=0.0,
            latency_ms=latency_ms,
            status=status,
            error=error_msg,
            request_json=_request_snapshot(kwargs),
            response_json={},
            response_summary=None,
        )
        raise

    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage = getattr(response, "usage", None)
    tokens_input = getattr(usage, "prompt_tokens", 0) or 0
    tokens_output = getattr(usage, "completion_tokens", 0) or 0
    tokens_cached = 0
    if usage is not None:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            tokens_cached = getattr(details, "cached_tokens", 0) or 0

    cost = await pricing.estimate_cost(
        model,
        kind="token",
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cached=tokens_cached,
    )
    summary, resp_dict = _response_summary(endpoint="chat.completions", response=response)
    await _persist_row(
        provider="openai",
        model=model,
        endpoint="chat.completions",
        source=source,
        project_id=project_id,
        org_id=org_id,
        analysis_run_id=analysis_run_id,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        tokens_cached=tokens_cached,
        cost_usd=cost,
        latency_ms=latency_ms,
        status=status,
        error=None,
        request_json=_request_snapshot(kwargs),
        response_json=resp_dict,
        response_summary=summary,
    )
    return response


async def tracked_embedding(
    client: AsyncOpenAI,
    *,
    source: str,
    project_id: UUID | None = None,
    org_id: UUID | None = None,
    analysis_run_id: int | None = None,
    **kwargs: Any,
) -> Any:
    """Call `client.embeddings.create` and record it; same contract as `tracked_chat_completion`."""
    killswitch.assert_not_killed()
    await limits.enforce_spend_limit(provider="openai")

    model = kwargs.get("model", "unknown")
    t0 = time.perf_counter()

    try:
        response = await client.embeddings.create(**kwargs)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await _persist_row(
            provider="openai",
            model=model,
            endpoint="embeddings",
            source=source,
            project_id=project_id,
            org_id=org_id,
            analysis_run_id=analysis_run_id,
            tokens_input=0,
            tokens_output=0,
            tokens_cached=0,
            cost_usd=0.0,
            latency_ms=latency_ms,
            status="error",
            error=repr(exc)[:2000],
            request_json=_request_snapshot(kwargs),
            response_json={},
            response_summary=None,
        )
        raise

    latency_ms = int((time.perf_counter() - t0) * 1000)
    usage = getattr(response, "usage", None)
    tokens_input = getattr(usage, "prompt_tokens", 0) or 0
    cost = await pricing.estimate_cost(model, kind="token", tokens_input=tokens_input)
    summary, resp_dict = _response_summary(endpoint="embeddings", response=response)

    await _persist_row(
        provider="openai",
        model=model,
        endpoint="embeddings",
        source=source,
        project_id=project_id,
        org_id=org_id,
        analysis_run_id=analysis_run_id,
        tokens_input=tokens_input,
        tokens_output=0,
        tokens_cached=0,
        cost_usd=cost,
        latency_ms=latency_ms,
        status="ok",
        error=None,
        request_json=_request_snapshot(kwargs),
        response_json=resp_dict,
        response_summary=summary,
    )
    return response


# ---------------------------------------------------------------------------
# Cohere rerank (cross-encoder)
# ---------------------------------------------------------------------------
def _cohere_request_snapshot(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return a request snapshot with the (often huge) `documents` list reduced to a summary.

    Only the document count and a sample are kept, so the row stays useful for replay and audit
    without ballooning. The `query` and the document sample are caller-derived text, so they are
    redacted unless `settings.conduit_capture_query_text` is on.
    """
    capture = get_settings().conduit_capture_query_text
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if k == "documents" and isinstance(v, list):
            out[k] = {
                "_truncated": True,
                "n_items": len(v),
                "sample": (v[0][:300] if v and isinstance(v[0], str) else "") if capture else "",
            }
        elif k == "query":
            out[k] = v if capture else _redact_text(v)
        elif k.lower() in _SENSITIVE_KWARG_KEYS:
            out[k] = _redact_sensitive_kwarg()
        else:
            out[k] = v
    return out


def _cohere_response_summary(response: Any) -> tuple[str, dict[str, Any]]:
    """Return a one-line summary and the full structured payload for replay."""
    try:
        d = (
            response.dict()
            if hasattr(response, "dict")
            else response.model_dump()
            if hasattr(response, "model_dump")
            else dict(response)
        )
    except Exception:
        d = {"_repr": repr(response)[:2000]}
    try:
        results = response.results or []
        top = results[0].relevance_score if results else None
        summary = (
            f"{len(results)} reranked, top={top:.4f}"
            if top is not None
            else f"{len(results)} reranked"
        )
    except Exception:
        summary = "(rerank)"
    return summary, d


async def tracked_cohere_rerank(
    client: cohere.AsyncClientV2,
    *,
    source: str,
    project_id: UUID | None = None,
    org_id: UUID | None = None,
    analysis_run_id: int | None = None,
    **kwargs: Any,
) -> Any:
    """Call `cohere.AsyncClientV2.rerank` and record it; same contract as the OpenAI wrappers.

    The kill switch and spend cap are checked before the call; cost and latency are recorded
    after it. Cost is a flat per-call price (`pricing.estimate_cost(kind="per_call")`). Cohere
    returns no token counts, so `tokens_input` records the number of documents to show the
    call's scale.
    """
    killswitch.assert_not_killed()
    await limits.enforce_spend_limit(provider="cohere")

    model = kwargs.get("model", "unknown")
    n_docs = len(kwargs.get("documents") or [])
    t0 = time.perf_counter()
    error_msg: str | None = None
    response: Any = None
    status = "ok"

    try:
        response = await client.rerank(**kwargs)
    except Exception as exc:
        status = "error"
        error_msg = repr(exc)[:2000]
        latency_ms = int((time.perf_counter() - t0) * 1000)
        await _persist_row(
            provider="cohere",
            model=model,
            endpoint="rerank",
            source=source,
            project_id=project_id,
            org_id=org_id,
            analysis_run_id=analysis_run_id,
            tokens_input=n_docs,
            tokens_output=0,
            tokens_cached=0,
            cost_usd=0.0,
            latency_ms=latency_ms,
            status=status,
            error=error_msg,
            request_json=_cohere_request_snapshot(kwargs),
            response_json={},
            response_summary=None,
        )
        raise

    latency_ms = int((time.perf_counter() - t0) * 1000)
    cost = await pricing.estimate_cost(model, kind="per_call")
    summary, resp_dict = _cohere_response_summary(response)
    await _persist_row(
        provider="cohere",
        model=model,
        endpoint="rerank",
        source=source,
        project_id=project_id,
        org_id=org_id,
        analysis_run_id=analysis_run_id,
        tokens_input=n_docs,
        tokens_output=0,
        tokens_cached=0,
        cost_usd=cost,
        latency_ms=latency_ms,
        status=status,
        error=None,
        request_json=_cohere_request_snapshot(kwargs),
        response_json=resp_dict,
        response_summary=summary,
    )
    return response
