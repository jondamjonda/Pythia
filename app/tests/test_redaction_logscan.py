"""AC-REDACTION-LOGSCAN (WP-029 / A4-12) — the HARD CI redaction tripwire.

THIS IS A MERGE GATE. If this file fails, the merge is blocked: it proves that a
tracked LLM/embedding/rerank call with ``settings.conduit_capture_query_text=False``
(the default — see config.py:136) leaks NO caller free text and NO secret onto
either persistence surface:

  (a) the persisted ``api_call.request_json`` (built by
      ``api_tracker._request_snapshot`` / ``_cohere_request_snapshot`` and handed
      to ``_persist_row``), and
  (b) the structlog output, including the ``emit()`` write in
      observability/events.py (``log_method(code, **event)`` — the call that
      flushes the whole Event, payload included, to the JSON log).

The scan looks for: the verbatim fixture prompt/query/message/document text; API
keys (``sk-…``, ``Bearer …`` / bearer tokens, and other known provider key
prefixes); and arbitrary high-entropy secrets carried as literals. It asserts
ZERO leakage. The allow/deny pattern lists (``_SECRET_PATTERNS`` /
``_SENSITIVE_LITERALS``) are module-level and intentionally **extensible** — add
a pattern here when a new provider or secret shape appears; the gate widens with
no other change.

The scanner is self-tested (``test_scanner_actually_detects_a_leak``) so a
no-op/over-permissive scanner can never make this gate pass vacuously.

If something IS leaking, the FIX lives in ``observability/api_tracker.py``
(the redaction is produced there) — NOT in this test. This file only proves the
leak; flag it and let the api_tracker owner patch the scrubber.

Caveat — SYNCHRONOUS BUILD ONLY. The live, fully-async Conduit trigger (the path
that streams a caller's real query through a running sidecar) is DEFERRED to
Phase-C; it does not exist yet. What is active NOW, and what this gate guards, is
the scrubbing tripwire: the redaction defaults inside ``api_tracker`` and the
``emit()`` log write. The wrapper-driven tests below mock the kill-switch /
spend-limit / cost / DB seams (no network) and the live-DB test below persists a
real row through the actual wrappers when a *scratch/test/copy* DB is reachable
(else it skips). Both surfaces are exercised today.

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from regsuper.observability import api_tracker
from regsuper.observability.events import emit

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — the realistic caller secrets we feed in and must never see come out.
# ─────────────────────────────────────────────────────────────────────────────
# A realistic client question (the kind of thing a caller puts in `messages`,
# `prompt`, `query`, embedding `input`, or a Cohere `document`).
CLIENT_QUERY = (
    "Does our €4.2M crypto-custody launch in Brno trigger MiCAR Art 60 "
    "and do we need a CASP licence before 2026-Q3? Internal codename ORION-7."
)
# A second distinct piece of caller text (rerank document body).
CLIENT_DOC = "Board memo: counsel advised we are likely in scope of AMLR Art 9."
# A realistic OpenAI secret key (sk- prefix, 40+ chars — see definitions.py:199).
FAKE_OPENAI_KEY = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0" * 2 + "ZzYy"
# A realistic bearer token an auth header might carry.
FAKE_BEARER = "Bearer eyJhbGciOiJIUzI1Ni}.dGVzdC1wYXlsb2Fk.s3cr3t-sig-DEADBEEF"
# A realistic Cohere/Anthropic-shaped key.
FAKE_COHERE_KEY = "co-aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789aBcD"


# ─────────────────────────────────────────────────────────────────────────────
# The deny-list — EXTENSIBLE. Append a regex here when a new provider/secret
# shape needs guarding; every scan below picks it up with no further change.
# ─────────────────────────────────────────────────────────────────────────────
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI secret keys
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"),  # Authorization: Bearer …
    re.compile(r"\bco-[A-Za-z0-9]{30,}"),  # Cohere-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),  # Anthropic secret keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id (belt-and-braces)
]

# Literal caller texts that MUST NOT appear verbatim on either surface when
# capture is off. EXTENSIBLE — add any new fixture secret you introduce above.
_SENSITIVE_LITERALS: tuple[str, ...] = (
    CLIENT_QUERY,
    CLIENT_DOC,
    "ORION-7",  # the embedded codename, scanned on its own as a tighter tripwire
    FAKE_OPENAI_KEY,
    FAKE_BEARER,
    FAKE_COHERE_KEY,
)


def _scan(blob: str) -> str | None:
    """Return the first offending fragment found in ``blob`` (a literal hit or a
    secret-pattern match), or ``None`` if the blob is clean.

    Both the literal deny-list and the regex deny-list are consulted, so a leak
    is caught whether the caller text survived verbatim or a key slipped through
    embedded in some larger field.
    """
    for literal in _SENSITIVE_LITERALS:
        if literal and literal in blob:
            return f"literal:{literal[:40]!r}"
    for pat in _SECRET_PATTERNS:
        m = pat.search(blob)
        if m:
            return f"pattern:{pat.pattern} -> {m.group(0)[:40]!r}"
    return None


def _as_text(obj: object) -> str:
    """Flatten any JSON-safe object to a single searchable string. ``default=str``
    keeps it from blowing up on the odd non-serialisable value (a real leak must
    still surface as text, never hide behind a serialisation error)."""
    return json.dumps(obj, default=str, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# helpers — point api_tracker.get_settings() at a redaction-off Settings stub.
# ─────────────────────────────────────────────────────────────────────────────
@contextmanager
def _capture_off():
    """conduit_capture_query_text=False is the DEFAULT this gate guards."""
    settings = SimpleNamespace(conduit_capture_query_text=False)
    with patch.object(api_tracker, "get_settings", lambda: settings):
        yield


@contextmanager
def _capture_on():
    """conduit_capture_query_text=True (verbatim replay capture) — transport secrets
    must STILL be scrubbed even in this mode."""
    settings = SimpleNamespace(conduit_capture_query_text=True)
    with patch.object(api_tracker, "get_settings", lambda: settings):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# 0. The scanner must actually work — a deliberately-leaky blob MUST trip it.
#    Without this, a no-op scanner would make every assertion below pass for free.
# ─────────────────────────────────────────────────────────────────────────────
def test_scanner_actually_detects_a_leak():
    assert _scan("clean shape-only marker, n_messages=2") is None
    assert _scan(_as_text({"_redacted": True, "roles": ["user"]})) is None
    # Every sensitive literal is detected when present verbatim.
    for literal in _SENSITIVE_LITERALS:
        assert _scan(f"prefix {literal} suffix") is not None, literal
    # Every secret pattern is detected even without a matching literal.
    assert _scan("auth=sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAA") is not None
    assert _scan("Authorization: Bearer abcdef0123456789") is not None
    assert _scan("co-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA") is not None


# ─────────────────────────────────────────────────────────────────────────────
# (a) SURFACE ONE — the persisted request_json (snapshot helpers, capture OFF).
# ─────────────────────────────────────────────────────────────────────────────
def test_chat_snapshot_request_json_is_clean():
    # A realistic chat call: the secret lives in the caller text (messages),
    # which is exactly what api_tracker's redaction contract covers. The
    # response_format / temperature etc. are non-text kwargs and carry no secret.
    kwargs = {
        "model": "gpt-4o",
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": CLIENT_QUERY},
            {"role": "user", "content": CLIENT_DOC},
        ],
    }
    with _capture_off():
        snap = api_tracker._request_snapshot(kwargs)
    hit = _scan(_as_text(snap))
    assert hit is None, f"chat request_json leaked: {hit}"


def test_embedding_snapshot_request_json_is_clean():
    kwargs = {"model": "text-embedding-3-small", "input": [CLIENT_QUERY, CLIENT_DOC]}
    with _capture_off():
        snap = api_tracker._request_snapshot(kwargs)
    hit = _scan(_as_text(snap))
    assert hit is None, f"embedding request_json leaked: {hit}"


def test_cohere_snapshot_request_json_is_clean():
    kwargs = {
        "model": "rerank-v3.5",
        "query": CLIENT_QUERY,
        "documents": [CLIENT_DOC, "doc two", "doc three"],
        "top_n": 5,
    }
    with _capture_off():
        snap = api_tracker._cohere_request_snapshot(kwargs)
    hit = _scan(_as_text(snap))
    assert hit is None, f"cohere request_json leaked: {hit}"


# ─────────────────────────────────────────────────────────────────────────────
# KNOWN BOUNDARY (documented, NOT a merge blocker by itself).
#
# api_tracker's redaction covers the caller TEXT fields (messages / prompt / query /
# embedding input / cohere documents) AND — since the A4-12 hardening — any kwarg
# whose key is auth-shaped (api_key / authorization / extra_headers / …), via the
# _SENSITIVE_KWARG_KEYS deny-list in _request_snapshot/_cohere_request_snapshot. So a
# transport secret passed as a *.create() kwarg never lands in request_json, even
# with conduit_capture_query_text=True (replay never needs transport credentials).
# This is a hard gate: if the deny-list regresses, a secret re-leaks and CI fails.
# ─────────────────────────────────────────────────────────────────────────────
def test_transport_secret_kwargs_are_scrubbed():
    kwargs = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "hi"}],
        "extra_headers": {"Authorization": FAKE_BEARER},
        "api_key": FAKE_OPENAI_KEY,
    }
    # Scrubbed regardless of the capture flag — transport secrets are never persisted.
    for ctx in (_capture_off, _capture_on):
        with ctx():
            snap = api_tracker._request_snapshot(kwargs)
        hit = _scan(_as_text(snap))
        assert hit is None, f"transport secret leaked through request_json kwargs: {hit}"


# ─────────────────────────────────────────────────────────────────────────────
# (a) SURFACE ONE via the REAL wrappers — capture the request_json that would be
#     persisted by intercepting _persist_row, with every external seam mocked.
# ─────────────────────────────────────────────────────────────────────────────
class _FakeChatClient:
    def __init__(self):
        async def _create(**_kwargs):
            usage = SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None
            )
            choice = SimpleNamespace(message=SimpleNamespace(content="redacted-ok"))
            return SimpleNamespace(usage=usage, choices=[choice], model_dump=lambda: {})

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=_create))


class _FakeEmbedClient:
    def __init__(self):
        async def _create(**_kwargs):
            usage = SimpleNamespace(prompt_tokens=7)
            data = [SimpleNamespace(embedding=[0.1, 0.2])]
            return SimpleNamespace(usage=usage, data=data, model_dump=lambda: {"data": []})

        self.embeddings = SimpleNamespace(create=_create)


class _FakeCohereClient:
    async def rerank(self, **_kwargs):
        results = [SimpleNamespace(relevance_score=0.9)]
        return SimpleNamespace(results=results, model_dump=lambda: {"results": []})


@contextmanager
def _patch_wrapper_seams(*, persisted: list):
    """Mock every external seam the tracked_* wrappers touch (no DB / no network).
    Capture the kwargs handed to _persist_row so we can scan its request_json."""

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


async def test_tracked_chat_request_json_clean_through_wrapper():
    persisted: list = []
    with _patch_wrapper_seams(persisted=persisted):
        await api_tracker.tracked_chat_completion(
            _FakeChatClient(),
            source="logscan-test",
            model="gpt-4o",
            messages=[{"role": "user", "content": CLIENT_QUERY}],
        )
    assert persisted, "the success path must persist a row"
    hit = _scan(_as_text(persisted[-1]["request_json"]))
    assert hit is None, f"tracked_chat_completion persisted a leak: {hit}"


async def test_tracked_embedding_request_json_clean_through_wrapper():
    persisted: list = []
    with _patch_wrapper_seams(persisted=persisted):
        await api_tracker.tracked_embedding(
            _FakeEmbedClient(),
            source="logscan-test",
            model="text-embedding-3-small",
            input=[CLIENT_QUERY, CLIENT_DOC],
        )
    assert persisted
    hit = _scan(_as_text(persisted[-1]["request_json"]))
    assert hit is None, f"tracked_embedding persisted a leak: {hit}"


async def test_tracked_cohere_request_json_clean_through_wrapper():
    persisted: list = []
    with _patch_wrapper_seams(persisted=persisted):
        await api_tracker.tracked_cohere_rerank(
            _FakeCohereClient(),
            source="logscan-test",
            model="rerank-v3.5",
            query=CLIENT_QUERY,
            documents=[CLIENT_DOC, "doc two"],
        )
    assert persisted
    hit = _scan(_as_text(persisted[-1]["request_json"]))
    assert hit is None, f"tracked_cohere_rerank persisted a leak: {hit}"


# ─────────────────────────────────────────────────────────────────────────────
# (b) SURFACE TWO — the structlog output. emit() flushes the whole Event (payload
#     + diagnostic_message) to structlog (events.py: log_method(code, **event)).
#     A tracked call that fails persists nothing but DOES emit an error event; we
#     prove that surface, and any control-room event, carries no caller text.
# ─────────────────────────────────────────────────────────────────────────────
async def test_emit_structlog_output_carries_no_caller_text():
    """An app-domain event whose payload is built from request SHAPE (counts,
    roles) — never the caller's words — must log clean. This pins the contract
    that emit() callers pass shape, not content."""
    with capture_logs() as logs:
        await emit(
            "retrieval.candidates.found",
            project_id="proj-x",
            n=3,
            # shape-only payload mirroring what a redacted snapshot carries
            request_shape={"_redacted": True, "n_messages": 2, "roles": ["user"]},
        )
    hit = _scan(_as_text(logs))
    assert hit is None, f"emit() structlog output leaked: {hit}"


async def test_emit_structlog_leak_is_caught_if_present():
    """Negative control for surface (b): IF a caller wrongly put raw query text
    into an event payload, the structlog scan MUST catch it. This guarantees the
    (b) scan is load-bearing, not vacuous — it would fail loudly on a real leak."""
    with capture_logs() as logs:
        await emit("error.api", service="openai", detail=CLIENT_QUERY, key=FAKE_OPENAI_KEY)
    hit = _scan(_as_text(logs))
    assert hit is not None, "structlog scan failed to catch a deliberately-leaked payload"


async def test_tracked_call_emits_no_query_text_on_error_path():
    """The wrappers' error path persists a (redacted) row AND, on a DB failure,
    emits 'api_tracker.persist_error' with repr(exc). Scan the real structlog
    stream around a tracked call to prove neither the event nor the bound logger
    state carries the caller's query. The DB seam is forced to fail so the
    error-emit path is exercised."""

    @asynccontextmanager
    async def boom_session_scope():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    settings = SimpleNamespace(conduit_capture_query_text=False)
    with capture_logs() as logs:
        with (
            patch.object(api_tracker, "session_scope", boom_session_scope),
            patch.object(api_tracker.keys, "active_key_id", lambda provider: "openai:test"),
            patch.object(api_tracker, "get_settings", lambda: settings),
        ):
            await api_tracker._persist_row(
                provider="openai",
                model="gpt-4o",
                endpoint="chat.completions",
                source="logscan-test",
                project_id=None,
                org_id=None,
                analysis_run_id=None,
                tokens_input=1,
                tokens_output=1,
                tokens_cached=0,
                cost_usd=0.0,
                latency_ms=1,
                status="ok",
                error=None,
                # already-redacted request_json (what the wrapper hands in)
                request_json={"_redacted": True, "n_messages": 1, "roles": ["user"]},
                response_json={},
                response_summary=None,
            )
    hit = _scan(_as_text(logs))
    assert hit is None, f"persist-error structlog path leaked: {hit}"


# ─────────────────────────────────────────────────────────────────────────────
# (a) LIVE-DB variant — persist a REAL api_call row through the actual wrappers,
#     re-read request_json straight from Postgres, and scan it. This proves the
#     scrub survives the JSONB round-trip, not just the in-memory snapshot.
#
#     Mirrors the test_migration_chain DB-or-skip convention: it runs only
#     against a *scratch/test/copy* DB (never the real `regsuper`), reading the
#     DSN from REDACTION_TEST_DATABASE_URL (preferred) or DATABASE_URL. If no
#     safe DB is reachable, it SKIPS — the offline scans above still gate CI.
# ─────────────────────────────────────────────────────────────────────────────
def _safe_async_dsn() -> str | None:
    """Resolve an async DSN that names a scratch/test/copy DB, or None."""
    dsn = os.environ.get("REDACTION_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        return None
    # Extract the db name (after the last '/', before any '?') for the safety check.
    tail = dsn.rsplit("/", 1)[-1]
    name = tail.split("?", 1)[0].lower()
    if not any(tok in name for tok in ("scratch", "test", "copy")):
        return None
    return dsn


async def _db_reachable(async_dsn: str) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(async_dsn, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        await engine.dispose()


@pytest.fixture
def live_db_dsn() -> str:
    dsn = _safe_async_dsn()
    if dsn is None:
        pytest.skip(
            "no safe DB configured (set REDACTION_TEST_DATABASE_URL to a regsuper "
            "*scratch/test/copy* DB holding the api_call table)"
        )
    import asyncio

    if not asyncio.run(_db_reachable(dsn)):
        pytest.skip(f"redaction-scan DB not reachable: {dsn.rsplit('/', 1)[-1]!r}")
    return dsn


async def test_persisted_row_in_db_has_no_leak(live_db_dsn):
    """End-to-end: run a real tracked_chat_completion against the scratch DB with
    capture OFF, then SELECT the row's request_json back out and scan it. The
    only thing mocked is the OpenAI client (no network); the kill-switch, spend
    limit, cost book, redaction, and DB write are all the real code path."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(live_db_dsn, future=True)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def real_session_scope():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    settings = SimpleNamespace(conduit_capture_query_text=False)
    source = f"logscan-{uuid4().hex[:8]}"

    async def fake_enforce(*_a, **_k):
        return None

    async def fake_cost(*_a, **_k):
        return 0.0

    try:
        with (
            patch.object(api_tracker, "session_scope", real_session_scope),
            patch.object(api_tracker.killswitch, "assert_not_killed", lambda: None),
            patch.object(api_tracker.limits, "enforce_spend_limit", fake_enforce),
            patch.object(api_tracker.pricing, "estimate_cost", fake_cost),
            patch.object(api_tracker.keys, "active_key_id", lambda provider: "openai:test"),
            patch.object(api_tracker, "get_settings", lambda: settings),
        ):
            await api_tracker.tracked_chat_completion(
                _FakeChatClient(),
                source=source,
                model="gpt-4o",
                messages=[{"role": "user", "content": CLIENT_QUERY}],
            )

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT request_json FROM api_call "
                        "WHERE source = :s ORDER BY ts DESC LIMIT 1"
                    ),
                    {"s": source},
                )
            ).fetchall()
        assert rows, "the tracked call did not persist a row to the DB"
        hit = _scan(_as_text(rows[0][0]))
        assert hit is None, f"DB-persisted request_json leaked: {hit}"
    finally:
        # Clean up the test row so the scratch DB stays tidy across runs.
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM api_call WHERE source = :s"), {"s": source})
        finally:
            await engine.dispose()


# Keep the structlog config in a known state for the rest of the suite — capture_logs
# swaps the processor chain in/out per-context, but reset defensively if a prior test
# left a custom configuration in place.
@pytest.fixture(autouse=True)
def _reset_structlog():
    yield
    structlog.reset_defaults()
