"""A2b (C4 PROFILE-CONTEXT transport) — the app side of threading a saved BusinessProfile
into the /api/answer intake call.

All DB-free / sidecar-mocked (the ``test_answer_endpoint`` pattern). What this pins:

  * BYTE-IDENTICAL OFF — with ``answer_profile_context_enabled=False`` (the default) the handler
    NEVER loads a profile and calls ``pythia.answer`` with ``project_context=None`` (the wire body
    omits the key), so today's requests are unchanged.
  * ON + a resolvable profile ⇒ the small WHITELISTED typed dict reaches the sidecar call.
  * ON but no project_id / an unresolvable profile ⇒ project_context stays None.
  * ``_serialize_answer_context`` emits ONLY the whitelisted typed fields — NO free-text / PII.
  * WP-029 — the client boundary NEVER persists project_context into ``api_call.request_json``
    (the cost row stays ``{}``), even though the value IS sent in the POST body.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.analysis.answer_context import AnswerContext
from regsuper.api import answer as answer_api
from regsuper.config import Settings, get_settings
from regsuper.pythia import client as pc

_ANON_ORG = UUID("00000000-0000-0000-0000-000000000001")
_PROJECT_ID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# Harness (mirrors test_answer_endpoint)
# ---------------------------------------------------------------------------
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(answer_api.router)
    return TestClient(app, raise_server_exceptions=True)


def _settings(*, answer_profile_context_enabled: bool = False) -> Settings:
    """Derive the test settings from the REAL ``Settings`` and override only the fields these
    profile-context tests pin. Deriving from the real object (vs a hand-built ``SimpleNamespace``)
    means EVERY flag the handler reads is present with its real default — so a newly-added flag can
    never AttributeError this stub again (the WS0/WS2 regression: the handler grew
    ``answer_practical_block_enabled`` and the frozen stub lacked it → 4 red tests)."""
    return get_settings().model_copy(
        update={
            "answer_endpoint_enabled": True,
            "compliance_guard_enabled": False,
            "compliance_guard_mode": "shadow",
            "require_auth": False,
            "entitlements_enabled": False,
            "answer_shadow_weak_coverage_hedged": False,
            "answer_profile_context_enabled": answer_profile_context_enabled,
        }
    )


def _sound_answer() -> dict:
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Legislativní rámec\nViz § 5 [1].",
        "cited_provisions": [{"provision_id": "p-1"}],
        "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
        "sections": [
            {
                "kind": "legislative",
                "heading": "Legislativní rámec",
                "text": "Viz § 5.",
                "citations_map": [{"start": 0, "end": 5, "provision_id": "p-1", "label": "§ 5"}],
            }
        ],
        "render_mode": "full",
        "band": "full",
        "meta": {"route": "sound", "intent": "legal", "coverage": "covered"},
    }


_WHITELIST_CTX = {
    "legal_form": "s.r.o.",
    "nace_code": "56.10",
    "derived_tags": ["gastronomy"],
    "headcount_band": "1-9",
    "turnover_band": "<2M",
    "jurisdiction": "CZ",
    "processes_personal_data": True,
    "handles_client_money": False,
    "has_real_estate": False,
    "cross_border": False,
    "is_employer": True,
    "municipality_code": "554782",
    "preferred_language": "cs",
}


# ═══════════════════════════════════════════════════════════════════════════
# 1. _serialize_answer_context — whitelist only, NO PII.
# ═══════════════════════════════════════════════════════════════════════════
def test_serialize_answer_context_whitelist_no_pii():
    """The serialiser emits EXACTLY the whitelisted typed keys and never any free-text/PII
    field (display_name, legal_name, ico, annual_turnover_czk, address …)."""
    ctx = AnswerContext(
        legal_form="s.r.o.",
        nace_code="56.10",
        derived_tags=["gastronomy", "employer"],
        headcount_band="1-9",
        turnover_band="<2M",
        jurisdiction="CZ",
        processes_personal_data=True,
        is_employer=True,
        municipality_code="554782",
        preferred_language="cs",
    )
    out = answer_api._serialize_answer_context(ctx)
    assert set(out) == {
        "legal_form",
        "nace_code",
        "derived_tags",
        "headcount_band",
        "turnover_band",
        "jurisdiction",
        "processes_personal_data",
        "handles_client_money",
        "has_real_estate",
        "cross_border",
        "is_employer",
        "municipality_code",
        "preferred_language",
    }
    # no PII / free-text field leaked
    for banned in (
        "display_name",
        "legal_name",
        "ico",
        "annual_turnover_czk",
        "registered_municipality",
        "address",
        "context_hash",
    ):
        assert banned not in out
    assert out["derived_tags"] == ["gastronomy", "employer"]
    assert out["is_employer"] is True


def test_serialize_from_business_profile_stub_excludes_pii():
    """A BusinessProfile-like stub carrying PII (legal_name, ico, turnover) serialises to the
    whitelist only — the PII never reaches the wire dict."""
    stub = SimpleNamespace(
        legal_form="s.r.o.",
        nace_code="56.10",
        headcount_band="1-9",
        annual_turnover_czk=1_500_000,
        primary_jurisdiction="CZ",
        processes_personal_data=True,
        is_employer=True,
        municipality_code="554782",
        preferred_language="cs",
        # PII that must NOT leak:
        legal_name="Tajná Firma s.r.o.",
        ico="12345678",
        display_name="Bob's Bistro",
        registered_address="Náměstí 1, Praha",
    )
    ctx = AnswerContext.from_business_profile(stub)
    out = answer_api._serialize_answer_context(ctx)
    blob = repr(out)
    assert "Tajná Firma" not in blob and "12345678" not in blob
    assert "Bob's Bistro" not in blob and "Náměstí" not in blob
    assert out["turnover_band"] == "<2M"  # the coarse band, never the raw amount


# ═══════════════════════════════════════════════════════════════════════════
# 2. Handler — flag OFF / ON / no-profile paths.
# ═══════════════════════════════════════════════════════════════════════════
def _patch(settings, sidecar, load_ctx=None):
    patches = [
        patch.object(answer_api, "get_settings", lambda: settings),
        patch.object(answer_api.pythia, "answer", sidecar),
    ]
    if load_ctx is not None:
        patches.append(patch.object(answer_api, "_load_profile_context", load_ctx))
    return patches


def _apply(patches):
    for p in patches:
        p.start()


def _undo(patches):
    for p in patches:
        p.stop()


def test_flag_off_sends_no_project_context():
    """OFF (default) ⇒ pythia.answer is called with project_context=None even when a project_id
    is supplied (byte-identical: the block is never entered, _load_profile_context never runs)."""
    sidecar = AsyncMock(return_value=_sound_answer())
    load_spy = AsyncMock(return_value=_WHITELIST_CTX)
    patches = _patch(_settings(answer_profile_context_enabled=False), sidecar, load_spy)
    _apply(patches)
    try:
        resp = _client().post("/api/answer", json={"query": "x", "project_id": _PROJECT_ID})
    finally:
        _undo(patches)
    assert resp.status_code == 200, resp.text
    _, kwargs = sidecar.call_args
    assert kwargs["project_context"] is None
    load_spy.assert_not_awaited()  # the profile is NEVER loaded on the OFF path


def test_flag_on_with_profile_passes_whitelisted_dict():
    """ON + a resolvable profile ⇒ the WHITELISTED dict reaches the sidecar call."""
    sidecar = AsyncMock(return_value=_sound_answer())
    load = AsyncMock(return_value=_WHITELIST_CTX)
    patches = _patch(_settings(answer_profile_context_enabled=True), sidecar, load)
    _apply(patches)
    try:
        resp = _client().post("/api/answer", json={"query": "x", "project_id": _PROJECT_ID})
    finally:
        _undo(patches)
    assert resp.status_code == 200, resp.text
    _, kwargs = sidecar.call_args
    assert kwargs["project_context"] == _WHITELIST_CTX
    # the loader was called with the request project_id + the principal org_id
    load.assert_awaited_once()
    call_args = load.call_args[0]
    assert call_args[0] == _PROJECT_ID
    assert call_args[1] == _ANON_ORG


def test_flag_on_no_project_id_sends_none():
    """ON but the request carries NO project_id ⇒ project_context None, loader never called."""
    sidecar = AsyncMock(return_value=_sound_answer())
    load = AsyncMock(return_value=_WHITELIST_CTX)
    patches = _patch(_settings(answer_profile_context_enabled=True), sidecar, load)
    _apply(patches)
    try:
        resp = _client().post("/api/answer", json={"query": "x"})
    finally:
        _undo(patches)
    assert resp.status_code == 200, resp.text
    _, kwargs = sidecar.call_args
    assert kwargs["project_context"] is None
    load.assert_not_awaited()


def test_flag_on_unresolvable_profile_sends_none():
    """ON + a project_id but the loader resolves nothing (unknown/cross-org/soft-deleted) ⇒
    project_context stays None (degrades to the byte-identical no-context answer)."""
    sidecar = AsyncMock(return_value=_sound_answer())
    load = AsyncMock(return_value=None)
    patches = _patch(_settings(answer_profile_context_enabled=True), sidecar, load)
    _apply(patches)
    try:
        resp = _client().post("/api/answer", json={"query": "x", "project_id": _PROJECT_ID})
    finally:
        _undo(patches)
    assert resp.status_code == 200, resp.text
    _, kwargs = sidecar.call_args
    assert kwargs["project_context"] is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. Client boundary — WP-029: project_context sent in the POST body, NEVER in the cost row.
# ═══════════════════════════════════════════════════════════════════════════
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakePool:
    def __init__(self, payload):
        self._payload = payload
        self.post_calls: list[tuple] = []

    async def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return _FakeResponse(self._payload)


@pytest.fixture
def _wired_client(monkeypatch):
    """Wire the client DB-free: a fake pool, base-url stub, gate OFF, and a persist spy that
    RECORDS the request_json it was asked to write (so we can assert it stays {})."""
    pool = _FakePool({"drafted_answer": "ok", "render_mode": "full"})
    monkeypatch.setattr(pc, "_pool", lambda: pool)
    monkeypatch.setattr(pc, "_base_url", lambda: "http://sidecar")
    monkeypatch.setattr(
        pc,
        "get_settings",
        lambda: SimpleNamespace(pythia_boundary_spend_gate_enabled=False),
    )

    persisted: list[dict] = []

    async def _spy_persist(**kwargs):
        persisted.append(kwargs)

    async def _noop_emit(*args, **kwargs):
        return None

    import regsuper.observability.api_tracker as _api_tracker
    import regsuper.observability.events as _events

    monkeypatch.setattr(_api_tracker, "_persist_row", _spy_persist)
    monkeypatch.setattr(_events, "emit", _noop_emit)
    return pool, persisted


@pytest.mark.asyncio
async def test_client_omits_project_context_when_none(_wired_client):
    """None ⇒ the POST body has NO project_context key (byte-identical to today's wire shape)."""
    pool, _ = _wired_client
    await pc.answer("q")  # project_context defaults to None
    assert len(pool.post_calls) == 1
    body = pool.post_calls[0][1]["json"]
    assert "project_context" not in body


@pytest.mark.asyncio
async def test_client_includes_project_context_when_set(_wired_client):
    """A supplied project_context IS included in the POST body (the intake seed)."""
    pool, _ = _wired_client
    await pc.answer("q", project_context=_WHITELIST_CTX)
    body = pool.post_calls[0][1]["json"]
    assert body["project_context"] == _WHITELIST_CTX


@pytest.mark.asyncio
async def test_client_never_persists_project_context_into_cost_row(_wired_client):
    """WP-029: even when project_context is sent, EVERY api_call row's request_json stays {} —
    no profile fact ever leaks into a cost row."""
    _, persisted = _wired_client
    await pc.answer("q", project_context=_WHITELIST_CTX, org_id=_ANON_ORG)
    assert persisted, "the boundary must write at least the transport row"
    for row in persisted:
        assert row.get("request_json") == {}, "project_context must NOT reach the cost row"
        # defence-in-depth: no whitelisted value bled into the row at all
        blob = repr(row)
        assert "s.r.o." not in blob and "554782" not in blob


def test_profile_load_is_before_quota_reserve_in_source():
    """VL-C-06 (WS5 step 44) ORDER PIN: the profile-context load is a pure READ that must run BEFORE the quota
    reserve — the reserve stays the LAST gate before spend, and the KNOWN-FACTS context is resolved first. Pins
    the call-site ordering in the handler source (previously violated: the load sat after the reserve)."""
    import inspect

    from regsuper.api import answer as answer_api

    src = inspect.getsource(answer_api.ask_answer)
    # Match the actual CALL sites (with `(`), not prose in the docstring/comments that names either call.
    load_call = "await _load_profile_context("
    reserve_call = "_limits.check_and_reserve_quota("
    assert load_call in src and reserve_call in src
    assert src.index(load_call) < src.index(reserve_call)
