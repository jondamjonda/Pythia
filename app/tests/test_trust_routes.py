"""B2b-14b — EU-AI-Act public trust posture (api/trust.py), router-only.

Three things are pinned here, all DB-free (we mount ONLY the trust router, so there
is no FastAPI lifespan and no DB connect; the one DB-touching route has its
``session_scope`` patched with an in-memory fake):

1. The three public GETs return 200 with their required fields. The load-bearing
   assertions: ``technical-doc.high_risk_classification`` is BYTE-EQUAL to
   ``settings.ai_act_high_risk_classification`` (proves the classification is the
   AUTHORED flag, not computed in code), and ``data-residency.training_on_user_data``
   is exactly ``False``.

2. ``POST /api/findings/{id}/request-review`` flips an ``unreviewed`` finding to
   ``review_requested`` and is idempotent (a second call is a no-op that returns the
   same state, no error). The principal, the finding load, and the session are mocked.

3. ``POST /admin/api/aiact/incident`` is fail-closed: WITHOUT the control token (and
   outside local dev) it refuses (401/403); WITH the token configured + supplied it
   logs and returns ``{status: 'logged'}``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import admin as admin_api
from regsuper.api import trust as trust_api
from regsuper.config import get_settings


def _client() -> TestClient:
    """A TestClient over JUST the trust router — no FastAPI lifespan, so no DB connect
    at import (mirrors test_law_source_gate._client)."""
    app = FastAPI()
    app.include_router(trust_api.router)
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# 1. Public transparency GETs
# ---------------------------------------------------------------------------
def test_data_residency_get() -> None:
    resp = _client().get("/api/trust/data-residency")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    settings = get_settings()
    assert body["compute_region"] == settings.data_residency_compute_region
    assert body["storage_region"] == settings.data_residency_storage_region
    assert body["dpa_reference"] == settings.openai_dpa_url
    assert body["model_inference_provider"]  # a non-empty settings-derived string
    # The fixed GDPR/DPA invariant: we never train on user data.
    assert body["training_on_user_data"] is False


def test_disclosure_get() -> None:
    resp = _client().get("/api/trust/disclosure")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product"] == "Polarius"
    assert body["nature"] == "AI-powered legal-information tool"
    assert body["not_legal_advice"] is True
    assert body["model"]  # a model string
    assert body["human_oversight_url"] == "/api/findings/{id}/request-review"
    assert body["dpa_url"] == get_settings().openai_dpa_url


def test_technical_doc_get_high_risk_is_verbatim_flag() -> None:
    """CARDINAL: the high-risk classification is the AUTHORED config string returned
    verbatim — NOT computed. Byte-equality proves flag-don't-engineer."""
    resp = _client().get("/api/trust/ai-act/technical-doc")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    settings = get_settings()

    assert body["product"] == "Polarius"
    assert body["version"]
    assert body["purpose"]
    # The load-bearing assertion: verbatim authored string, not derived in code.
    assert body["high_risk_classification"] == settings.ai_act_high_risk_classification
    assert isinstance(body["known_limitations"], list) and body["known_limitations"]
    assert body["intended_use"]
    assert body["out_of_scope"]
    assert body["human_oversight"]
    assert body["contact"] == settings.operator_contact_email
    # The technical doc embeds the same residency object — no drift.
    assert body["data_residency"]["training_on_user_data"] is False
    assert body["data_residency"]["compute_region"] == settings.data_residency_compute_region


# ---------------------------------------------------------------------------
# 2. Art. 14 human oversight — request-review (mocked session + principal)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _fake_scope(session):
    yield session


class _FakeFinding:
    def __init__(self, review_state: str) -> None:
        self.review_state = review_state
        self.project_id = uuid4()
        self.org_id = uuid4()


class _FakeSession:
    """Minimal async session: ``get`` returns a preset finding (or None)."""

    def __init__(self, finding) -> None:
        self._finding = finding

    async def get(self, _model, _pk):
        return self._finding


def _patch_review(finding):
    """Patch get_principal, session_scope (→ fake session yielding ``finding``),
    log_event and emit so the route runs without a DB."""
    fake_session = _FakeSession(finding)

    async def fake_principal(_request=None):
        return SimpleNamespace(
            user_id=uuid4(), org_id=uuid4(), role="owner", is_platform_admin=False
        )

    async def fake_log_event(*_a, **_k):
        return None

    async def fake_emit(*_a, **_k):
        return None

    return (
        patch.object(trust_api, "get_principal", fake_principal),
        patch.object(trust_api, "session_scope", lambda: _fake_scope(fake_session)),
        patch.object(trust_api, "log_event", fake_log_event),
        patch.object(trust_api, "emit", fake_emit),
    )


def test_request_review_unreviewed_to_requested() -> None:
    finding = _FakeFinding("unreviewed")
    fid = uuid4()
    p_princ, p_scope, p_log, p_emit = _patch_review(finding)
    with p_princ, p_scope, p_log, p_emit:
        resp = _client().post(f"/api/findings/{fid}/request-review", json={"reason": "looks wrong"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_state"] == "review_requested"
    assert finding.review_state == "review_requested"


def test_request_review_is_idempotent() -> None:
    """A finding already in 'review_requested' is returned unchanged — no error."""
    finding = _FakeFinding("review_requested")
    fid = uuid4()
    p_princ, p_scope, p_log, p_emit = _patch_review(finding)
    with p_princ, p_scope, p_log, p_emit:
        resp = _client().post(f"/api/findings/{fid}/request-review", json={"reason": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_state"] == "review_requested"
    assert finding.review_state == "review_requested"  # untouched


def test_request_review_404_when_missing() -> None:
    fid = uuid4()
    p_princ, p_scope, p_log, p_emit = _patch_review(None)  # session.get → None
    with p_princ, p_scope, p_log, p_emit:
        resp = _client().post(f"/api/findings/{fid}/request-review", json={})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 3. Art. 72 incident — ADMIN-gated, fail-closed
# ---------------------------------------------------------------------------
_INCIDENT_BODY = {
    "description": "Model emitted an uncited claim in one finding.",
    "severity": "serious",
    "operator_assessment": "Isolated; citation guard now blocks it.",
    "corrective_action": "Patched the post-processor.",
}


def test_incident_fail_closed_without_token() -> None:
    """No control token configured + a non-local environment → the EXISTING control
    gate refuses BEFORE the body runs (fail-closed). 401 or 403."""
    closed_settings = SimpleNamespace(
        control_panel_token="",
        environment="prod",
        control_room_open_when_unset=False,
    )
    with patch.object(admin_api, "get_settings", lambda: closed_settings):
        resp = _client().post("/admin/api/aiact/incident", json=_INCIDENT_BODY)
    # require_control_token raises 503 when token unset outside local; either way it is
    # NOT a 200 and NOT a 500 — the gate refused.
    assert resp.status_code in (401, 403, 503), resp.text
    assert resp.status_code != 200


def test_incident_logged_with_token() -> None:
    """With the control token configured AND supplied in the header, plus the DB write
    path mocked, the incident is accepted and returns {status: 'logged'}."""
    token = "test-control-token"
    open_settings = SimpleNamespace(
        control_panel_token=token,
        environment="prod",
        control_room_open_when_unset=False,
    )

    fake_session = _FakeSession(None)

    async def fake_log_event(*_a, **_k):
        return None

    async def fake_emit(*_a, **_k):
        return None

    with (
        patch.object(admin_api, "get_settings", lambda: open_settings),
        patch.object(trust_api, "session_scope", lambda: _fake_scope(fake_session)),
        patch.object(trust_api, "log_event", fake_log_event),
        patch.object(trust_api, "emit", fake_emit),
    ):
        resp = _client().post(
            "/admin/api/aiact/incident",
            json=_INCIDENT_BODY,
            headers={"X-Control-Token": token},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "logged"
    assert body["incident_id"]
