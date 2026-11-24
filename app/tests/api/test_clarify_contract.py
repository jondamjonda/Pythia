"""VL-A-08 / VL-A-09 (A3.3) — the CLARIFY CONTRACT completion + the STAGE-2 success-path refund.

Now that the ENGINE can return render_mode="clarify" (VL-A-08), the post-engine contract must carry it through
_shape_answer WITHOUT ever downgrading it to a refusal. Pins:
  • "clarify" is in _ENGINE_TO_APP_RENDER_MODE and maps to the app "clarify".
  • an engine clarify answer through _shape_answer yields refused=False + render_mode=="clarify" + a disclaimer.
  • "clarify" is NOT in _NON_SOUND_MODES (so refused stays False).
  • disjointness — a hedged/refuse answer is NEVER clarify, and a clarify is NEVER hedged/refuse.
  • a CHIP-LESS clarify frame is never downgraded to refuse by AC-CITATION-CHECK.
  • `assumptions` defaults [] (byte-identical when absent).
  • VL-A-08 SUCCESS-PATH REFUND — a clarify engine response refunds the reserved unit (net-zero); a normal
    answer does NOT refund; a transport failure still refunds (unchanged).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import answer as answer_api
from regsuper.conduit.envelope import RefusalReason

_ANON_ORG = UUID("00000000-0000-0000-0000-000000000001")


# ---------------------------------------------------------------------------
# Engine answer fixtures
# ---------------------------------------------------------------------------
def _clarify_answer() -> dict:
    """An engine PythiaAnswer dict for the VL-A-08 stage-2 clarify: refused=False, verdict=None, an empty
    drafted_answer, render_mode="clarify", NO chips, and the intake_clarify audit fragment."""
    return {
        "refused": False,
        "verdict": None,
        "drafted_answer": "",
        "cited_provisions": [],
        "citations_map": [],
        "sections": [],
        "render_mode": "clarify",
        "band": "clarify",
        "meta": None,
        "audit_trace": {
            "matter_type": "employment_first_hire",
            "intake_clarify": {"sufficiency": "ambiguous", "max_cos": 0.12, "cos_floor": 0.55},
        },
    }


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


def _hedged_answer() -> dict:
    a = _sound_answer()
    a["render_mode"] = "hedged"
    a["band"] = "hedged"
    return a


def _refuse_answer() -> dict:
    return {
        "refused": True,
        "verdict": "abstain",
        "drafted_answer": "",
        "cited_provisions": [],
        "citations_map": [],
        "sections": [],
        "render_mode": "refuse",
        "band": "refuse",
        "meta": None,
        "audit_trace": {},
    }


# ---------------------------------------------------------------------------
# 1. mapping-table pins
# ---------------------------------------------------------------------------
def test_clarify_in_engine_to_app_map() -> None:
    assert answer_api._ENGINE_TO_APP_RENDER_MODE["clarify"] == "clarify"


def test_clarify_not_in_non_sound_modes() -> None:
    assert "clarify" not in answer_api._NON_SOUND_MODES


def test_clarify_maps_to_needs_clarification_reason() -> None:
    assert answer_api._RENDER_MODE_TO_REFUSAL_REASON["clarify"] == RefusalReason.NEEDS_CLARIFICATION


# ---------------------------------------------------------------------------
# 2. _shape_answer clarify pins
# ---------------------------------------------------------------------------
def test_shape_engine_clarify_is_not_refused() -> None:
    out = answer_api._shape_answer(_clarify_answer(), lang="cs")
    assert out.refused is False
    assert out.render_mode == "clarify"
    assert out.band == "clarify"
    assert out.disclaimer  # the constant information-not-advice disclaimer is stamped
    assert out.reason == str(RefusalReason.NEEDS_CLARIFICATION)
    assert out.matter_type == "employment_first_hire"
    # a clarify surfaces NO synthesized sections/chips
    assert out.sections == []
    assert out.chips == []


def test_chipless_clarify_never_downgraded_to_refuse() -> None:
    """AC-CITATION-CHECK all-fail downgrades a chip-less SOUND draft to refuse — a clarify (chip-less by
    construction) must be SIDESTEPPED and never become a refuse."""
    out = answer_api._shape_answer(_clarify_answer(), lang="cs")
    assert out.render_mode == "clarify"
    assert out.refused is False


def test_assumptions_default_empty() -> None:
    out = answer_api._shape_answer(_sound_answer(), lang="cs")
    assert out.assumptions == []


# ---------------------------------------------------------------------------
# 3. disjointness — clarify vs hedged/refuse
# ---------------------------------------------------------------------------
def test_hedged_is_never_clarify() -> None:
    out = answer_api._shape_answer(_hedged_answer(), lang="cs")
    assert out.render_mode == "hedged"
    assert out.render_mode != "clarify"
    assert out.refused is False


def test_refuse_is_never_clarify() -> None:
    out = answer_api._shape_answer(_refuse_answer(), lang="cs")
    assert out.render_mode == "refuse"
    assert out.render_mode != "clarify"
    assert out.refused is True


def test_clarify_is_never_hedged_or_refuse() -> None:
    out = answer_api._shape_answer(_clarify_answer(), lang="cs")
    assert out.render_mode not in ("hedged", "refuse")


# ---------------------------------------------------------------------------
# 4. VL-A-08 success-path refund (handler-level, DB-free)
# ---------------------------------------------------------------------------
def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(answer_api.router)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        answer_endpoint_enabled=True,
        compliance_guard_enabled=False,
        compliance_guard_mode="shadow",
        require_auth=False,
        entitlements_enabled=False,
        answer_shadow_weak_coverage_hedged=False,
        answer_profile_context_enabled=False,
        answer_clarify_enabled=False,
        answer_practical_block_enabled=False,
        answer_intake_panel_enabled=False,
        authority_registry_enabled=False,
    )


def _fake_session_scope():
    @asynccontextmanager
    async def _cm():
        yield object()

    return _cm()


def _run_answer_refund_test(engine_answer, *, raise_exc=None):
    """Drive POST /api/answer with entitlements resolving (so a unit is reserved). Returns (resp, refunds)."""
    from regsuper.observability import limits as limits_mod

    refunds: list[str] = []

    async def spy_answer(*args, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return engine_answer

    async def spy_reserve(session, org_id, metric, *, limit, now=None):
        return 1

    async def spy_refund(session, *, org_id, reserved_metrics):
        refunds.append(",".join(reserved_metrics))

    ent = SimpleNamespace(queries_per_day=10, analyses_per_month=3, plan_code="lite")

    async def fake_resolve(org_id, *, session=None):
        return ent

    # A raised transport error propagates to a 500 through the router; don't let the TestClient re-raise it.
    client = _client(raise_server_exceptions=raise_exc is None)
    with (
        patch.object(answer_api, "get_settings", lambda: _settings()),
        patch.object(answer_api.pythia, "answer", spy_answer),
        patch.object(answer_api.metering, "resolve_for_entry", fake_resolve),
        patch.object(answer_api.metering, "refund_for_run", spy_refund),
        patch.object(limits_mod, "check_and_reserve_quota", spy_reserve),
        patch.object(answer_api, "session_scope", lambda: _fake_session_scope()),
    ):
        resp = client.post("/api/answer", json={"query": "musím vést evidenci?"})
    return resp, refunds


def test_clarify_response_refunds_reserved_unit() -> None:
    """A clarify engine response consumed no billable synthesis → the reserved `queries` unit is refunded
    exactly once (net-zero quota for a clarified turn)."""
    resp, refunds = _run_answer_refund_test(_clarify_answer())
    assert resp.status_code == 200, resp.text
    assert resp.json()["render_mode"] == "clarify"
    assert resp.json()["refused"] is False
    assert refunds == ["queries"], "the reserved queries unit must be refunded on a clarify"


def test_normal_answer_does_not_refund() -> None:
    """A normal (sound) answer consumed a billable synthesis → NO refund."""
    resp, refunds = _run_answer_refund_test(_sound_answer())
    assert resp.status_code == 200, resp.text
    assert resp.json()["render_mode"] == "full"
    assert refunds == [], "a normal answer must NOT be refunded"


def test_transport_failure_still_refunds() -> None:
    """A transport failure (engine raises) still refunds the reserved unit (unchanged behaviour)."""
    resp, refunds = _run_answer_refund_test(None, raise_exc=RuntimeError("sidecar down"))
    assert resp.status_code == 500
    assert refunds == ["queries"], "a transport failure must still refund the reserved unit"
