"""Redaction default + tenancy/run threading — WP-029 + WP-030 (A4-11).

api_tracker must NOT persist caller free text (chat `messages`, `prompt`, `query`,
embedding `input` sample, Cohere `documents` sample) verbatim by DEFAULT. It stores
a content-free redaction marker unless settings.conduit_capture_query_text is True,
in which case the verbatim form is restored (the operator opt-in).

It must also accept the additive WP-030 tenancy/run params (org_id, analysis_run_id,
both default None) on every tracked_* wrapper, thread them onto the api_call row,
and NEVER raise out of the hot path even if the DB write blows up.

Everything is offline: the snapshot helpers are pure (they read get_settings()),
and the wrapper tests mock the kill-switch / spend-limit / DB / cost seams so no DB
or network is touched.

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from regsuper.observability import api_tracker


# --------------------------------------------------------------------------- helpers
@contextmanager
def _capture(*, capture_query_text: bool):
    """Point api_tracker.get_settings() at a Settings stub with the redaction flag
    set as requested. The snapshot helpers read only conduit_capture_query_text."""
    settings = SimpleNamespace(conduit_capture_query_text=capture_query_text)
    with patch.object(api_tracker, "get_settings", lambda: settings):
        yield


def _dumps_ok(obj) -> str:
    """A persisted snapshot must be JSON-serialisable (it lands in a JSONB column)."""
    return json.dumps(obj)


SECRET = "TOP-SECRET-CLIENT-QUESTION-12345"


# --------------------------------------------------------------------------- _request_snapshot redaction
def test_chat_messages_redacted_by_default():
    kwargs = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": SECRET},
            {"role": "user", "content": SECRET},
        ],
    }
    with _capture(capture_query_text=False):
        snap = api_tracker._request_snapshot(kwargs)
    blob = _dumps_ok(snap)
    assert SECRET not in blob, "verbatim message content leaked with capture OFF"
    assert snap["messages"]["_redacted"] is True
    assert snap["messages"]["n_messages"] == 2
    assert snap["messages"]["roles"] == ["system", "user"]  # roles kept (non-sensitive)
    assert snap["model"] == "gpt-4o"  # non-text kwargs untouched


def test_chat_messages_verbatim_when_capture_on():
    kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": SECRET}]}
    with _capture(capture_query_text=True):
        snap = api_tracker._request_snapshot(kwargs)
    assert snap["messages"] == kwargs["messages"]  # verbatim restored
    assert SECRET in _dumps_ok(snap)


def test_prompt_and_query_redacted_by_default():
    kwargs = {"model": "gpt-4o", "prompt": SECRET, "query": SECRET}
    with _capture(capture_query_text=False):
        snap = api_tracker._request_snapshot(kwargs)
    blob = _dumps_ok(snap)
    assert SECRET not in blob
    assert snap["prompt"]["_redacted"] is True
    assert snap["query"]["_redacted"] is True
    assert snap["prompt"]["length"] == len(SECRET)  # shape kept, content gone


def test_embedding_input_sample_dropped_by_default():
    kwargs = {"model": "text-embedding-3-small", "input": [SECRET, "another"]}
    with _capture(capture_query_text=False):
        snap = api_tracker._request_snapshot(kwargs)
    assert snap["input"]["_truncated"] is True
    assert snap["input"]["n_items"] == 2  # count kept (non-sensitive)
    assert snap["input"]["sample"] == ""  # the sample text is dropped
    assert SECRET not in _dumps_ok(snap)


def test_embedding_input_sample_restored_when_capture_on():
    kwargs = {"model": "text-embedding-3-small", "input": [SECRET, "another"]}
    with _capture(capture_query_text=True):
        snap = api_tracker._request_snapshot(kwargs)
    assert snap["input"]["sample"] == SECRET[:300]


# --------------------------------------------------------------------------- _cohere_request_snapshot redaction
def test_cohere_query_and_doc_sample_redacted_by_default():
    kwargs = {
        "model": "rerank-v3.5",
        "query": SECRET,
        "documents": [SECRET, "doc2", "doc3"],
        "top_n": 5,
    }
    with _capture(capture_query_text=False):
        snap = api_tracker._cohere_request_snapshot(kwargs)
    blob = _dumps_ok(snap)
    assert SECRET not in blob, "Cohere query/doc content leaked with capture OFF"
    assert snap["query"]["_redacted"] is True
    assert snap["documents"]["n_items"] == 3  # count kept
    assert snap["documents"]["sample"] == ""  # doc text dropped
    assert snap["top_n"] == 5  # non-text kwargs untouched


def test_cohere_query_and_doc_sample_restored_when_capture_on():
    kwargs = {"model": "rerank-v3.5", "query": SECRET, "documents": [SECRET, "doc2"]}
    with _capture(capture_query_text=True):
        snap = api_tracker._cohere_request_snapshot(kwargs)
    assert snap["query"] == SECRET
    assert snap["documents"]["sample"] == SECRET[:300]


# --------------------------------------------------------------------------- WP-030 param threading
class _FakeChatClient:
    """Minimal stand-in for AsyncOpenAI exposing chat.completions.create."""

    def __init__(self):
        async def _create(**_kwargs):
            usage = SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None
            )
            choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
            return SimpleNamespace(usage=usage, choices=[choice], model_dump=lambda: {})

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


@contextmanager
def _patch_wrapper_seams(*, persisted: list):
    """Mock every external seam the tracked_* wrappers touch so no DB / network is
    needed. Capture the kwargs handed to _persist_row in `persisted`."""

    async def fake_persist(**kw):
        persisted.append(kw)
        return uuid4()

    async def fake_enforce(*_a, **_k):
        return None

    async def fake_cost(*_a, **_k):
        return 0.0

    async def fake_emit(*_a, **_k):
        return None

    settings = SimpleNamespace(conduit_capture_query_text=False)

    with (
        patch.object(api_tracker, "_persist_row", fake_persist),
        patch.object(api_tracker.killswitch, "assert_not_killed", lambda: None),
        patch.object(api_tracker.limits, "enforce_spend_limit", fake_enforce),
        patch.object(api_tracker.pricing, "estimate_cost", fake_cost),
        patch.object(api_tracker, "emit", fake_emit),
        patch.object(api_tracker, "get_settings", lambda: settings),
    ):
        yield


async def test_org_and_run_params_thread_onto_row():
    persisted: list = []
    org = uuid4()
    with _patch_wrapper_seams(persisted=persisted):
        await api_tracker.tracked_chat_completion(
            _FakeChatClient(),
            source="test",
            org_id=org,
            analysis_run_id=77,
            model="gpt-4o",
            messages=[{"role": "user", "content": SECRET}],
        )
    assert persisted, "the success path must persist a row"
    row = persisted[-1]
    assert row["org_id"] == org
    assert row["analysis_run_id"] == 77
    # redaction still applies on the threaded path
    assert SECRET not in json.dumps(row["request_json"])


async def test_params_default_none_back_compat():
    # Omitting the new params (today's call-sites) must default both to None.
    persisted: list = []
    with _patch_wrapper_seams(persisted=persisted):
        await api_tracker.tracked_chat_completion(
            _FakeChatClient(),
            source="test",
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )
    row = persisted[-1]
    assert row["org_id"] is None
    assert row["analysis_run_id"] is None


# --------------------------------------------------------------------------- swallow path never raises
async def test_persist_row_swallows_db_error(monkeypatch):
    # The real _persist_row must NEVER raise out of the hot path even if the DB blows up.
    @asynccontextmanager
    async def boom_session_scope():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    captured: list = []

    async def fake_emit(code, *_a, **_k):
        captured.append(code)

    with (
        patch.object(api_tracker, "session_scope", boom_session_scope),
        patch.object(api_tracker, "emit", fake_emit),
        patch.object(api_tracker.keys, "active_key_id", lambda provider: "openai:test"),
    ):
        # Must return None, not raise.
        result = await api_tracker._persist_row(
            provider="openai",
            model="gpt-4o",
            endpoint="chat.completions",
            source="test",
            project_id=None,
            org_id=uuid4(),
            analysis_run_id=5,
            tokens_input=1,
            tokens_output=1,
            tokens_cached=0,
            cost_usd=0.0,
            latency_ms=1,
            status="ok",
            error=None,
            request_json={},
            response_json={},
            response_summary=None,
        )
    assert result is None
    assert "api_tracker.persist_error" in captured
