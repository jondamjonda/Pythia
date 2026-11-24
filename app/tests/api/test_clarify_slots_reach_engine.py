"""VL-A-07 (WS4 step 31) — resubmitted clarify slots reach the engine (were validated-then-DISCARDED).

Pins: a follow-up turn carrying `slots` resolves them into the engine's KNOWN-FACTS project_context (so turn 2's
request body DIFFERS from turn 1) and NEVER re-clarifies (max 1 round).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import answer as answer_api
from regsuper.config import get_settings

_EMPLOY_Q = "Chci v Plzni zaměstnat prvního zaměstnance do restaurace."


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(answer_api.router)
    return TestClient(app, raise_server_exceptions=True)


def _settings():
    return get_settings().model_copy(
        update={
            "answer_endpoint_enabled": True,
            "compliance_guard_enabled": False,
            "compliance_guard_mode": "shadow",
            "require_auth": False,
            "entitlements_enabled": False,
            "answer_shadow_weak_coverage_hedged": False,
            "answer_profile_context_enabled": False,
            "answer_clarify_enabled": True,
        }
    )


def _sound() -> dict:
    return {
        "refused": False,
        "verdict": "ok",
        "drafted_answer": "## Hodnocení\nOK.",
        "cited_provisions": [],
        "citations_map": [],
        "sections": [],
        "render_mode": "full",
        "band": "full",
        "meta": {"route": "sound"},
        "audit_trace": {},
    }


def _post(body: dict, sidecar: AsyncMock):
    patches = [
        patch.object(answer_api, "get_settings", lambda: _settings()),
        patch.object(answer_api.pythia, "answer", sidecar),
    ]
    for p in patches:
        p.start()
    try:
        return _client().post("/api/answer", json=body)
    finally:
        for p in patches:
            p.stop()


def test_followup_slots_reach_engine_and_never_reclarify():
    sidecar = AsyncMock(return_value=_sound())
    resp = _post(
        {"query": _EMPLOY_Q, "slots": {"is_employer": "yes", "headcount_band": "1"}}, sidecar
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["render_mode"] != "clarify"  # a follow-up NEVER re-clarifies (max 1 round)
    ctx = sidecar.call_args.kwargs["project_context"]
    assert (
        isinstance(ctx, dict) and ctx.get("is_employer") == "yes"
    )  # the slot facts reached the engine


def test_request_body_differs_with_vs_without_slots():
    # WITHOUT slots (first turn) → the handler may clarify; if it answers, project_context is None.
    spy_no = AsyncMock(return_value=_sound())
    r1 = _post({"query": _EMPLOY_Q}, spy_no)
    ctx_without = spy_no.call_args.kwargs["project_context"] if spy_no.call_args else "clarified"

    spy_yes = AsyncMock(return_value=_sound())
    _post({"query": _EMPLOY_Q, "slots": {"is_employer": "yes", "headcount_band": "1"}}, spy_yes)
    ctx_with = spy_yes.call_args.kwargs["project_context"]

    assert ctx_with != ctx_without  # turn 2 body differs from turn 1
    assert r1.json()["render_mode"] == "clarify" or ctx_without is None
