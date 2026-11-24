"""B2b-04 (§8.C-1) — the lawful-lane INPUT gate on /analyze.

The gate classifies the project's *declared intent* BEFORE any cascade spend. Its whole safety
contract is mode-dependent and must be exact:

  * SHADOW (the default, and the only mode on today until the G14 lawyer sign-off): the gate
    classifies + logs but NEVER blocks. The cascade runs and the response is byte-identical to a
    build without the gate. A reserved-advice verdict in shadow changes nothing the caller sees.
  * ENFORCE (off today; flipped only after G14): a `reserved_advice` verdict short-circuits to a
    200 ComplianceRefusalResponse + a lawyer handoff — NO cascade, NO finding (§16.3). Any other
    verdict (information / needs_review) proceeds into the cascade normally.

These are DB-free: we mount ONLY the projects router (no lifespan → no Postgres) and patch the
classifier, the project lookup, the layer entrypoints, and the handoff side-effects.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import handoff as handoff_api
from regsuper.api import projects as projects_api


def _client() -> TestClient:
    """A TestClient over JUST the projects router — no FastAPI lifespan, so no DB connect at import."""
    app = FastAPI()
    app.include_router(projects_api.router)
    return TestClient(app, raise_server_exceptions=True)


def _fake_l2_report() -> SimpleNamespace:
    """A minimal L2RunReport stand-in carrying every field L2Section reads."""
    return SimpleNamespace(
        n_candidates=0,
        n_evaluated=0,
        n_skipped_cached=0,
        n_applicable=0,
        n_ambiguous=0,
        n_not_applicable=0,
        n_refused=0,
        n_errors=0,
        findings=[],
        sample_errors=[],
        total_latency_ms=0,
    )


def _settings(*, enabled: bool, mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        law_source="regsuper",
        compliance_guard_enabled=enabled,
        compliance_guard_mode=mode,
    )


def _verdict(mode: str) -> SimpleNamespace:
    return SimpleNamespace(answer_mode=mode, reason=f"classified as {mode}")


@asynccontextmanager
async def _fake_session_scope():
    yield SimpleNamespace()


# --------------------------------------------------------------------------- SHADOW: never blocks
def test_shadow_reserved_advice_classifies_but_does_not_block():
    """SHADOW + a reserved-advice verdict: the classifier IS called (so the audit row is written), but
    the cascade STILL runs and the caller gets the normal AnalyzeResponse — byte-stable, no refusal."""
    pid = uuid4()
    called: dict[str, object] = {}

    async def spy_classify(*, text, project_id, where, org_id=None):
        called["classified"] = (text, where)
        return _verdict("reserved_advice")

    async def spy_l2(*_a, **_k):
        called["l2_ran"] = True
        return _fake_l2_report()

    async def fake_get_project(_pid):
        return SimpleNamespace(declared_intent="Mám zažalovat svého dodavatele?")

    async def fake_emit(*_a, **_k):
        return None

    async def fake_health():
        return {}

    with (
        patch.object(projects_api, "get_settings", lambda: _settings(enabled=True, mode="shadow")),
        patch.object(projects_api, "get_project", fake_get_project),
        patch.object(projects_api, "classify_answer_mode", spy_classify),
        patch.object(projects_api, "run_l2", spy_l2),
        patch.object(projects_api, "emit", fake_emit),
        patch.object(projects_api.call_stats, "citation_health", fake_health),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Normal AnalyzeResponse, NOT a ComplianceRefusalResponse.
    assert body["project_id"] == str(pid)
    assert "answer_mode" not in body
    assert called.get("l2_ran") is True  # the cascade ran despite the reserved verdict
    assert called.get("classified") is not None  # but it WAS classified (audit/shadow logging)


# --------------------------------------------------------------------------- ENFORCE: short-circuit
def test_enforce_reserved_advice_short_circuits_with_handoff():
    """ENFORCE + reserved-advice: 200 ComplianceRefusalResponse, the cascade does NOT run, a handoff is
    offered, and the run is recorded failed('reserved_advice') so the refusal is auditable."""
    pid = uuid4()
    state: dict[str, object] = {}

    async def spy_classify(*, text, project_id, where, org_id=None):
        return _verdict("reserved_advice")

    async def spy_l2(*_a, **_k):
        state["l2_ran"] = True  # must NOT happen
        return _fake_l2_report()

    async def fake_get_project(_pid):
        return SimpleNamespace(declared_intent="Je tato smlouva pro mě právně závazná?")

    handoff_id = uuid4()

    async def fake_create_offer(_session, **kw):
        state["handoff_trigger"] = kw.get("trigger")
        return SimpleNamespace(id=handoff_id)

    real_fail = projects_api.cascade_tracker.fail_run

    def spy_fail(run, layer, error):
        state["failed_layer"] = layer
        return real_fail(run, layer, error)

    async def fake_emit(code, *_a, **_k):
        state.setdefault("events", []).append(code)  # type: ignore[union-attr]

    with (
        patch.object(projects_api, "get_settings", lambda: _settings(enabled=True, mode="enforce")),
        patch.object(projects_api, "get_project", fake_get_project),
        patch.object(projects_api, "classify_answer_mode", spy_classify),
        patch.object(projects_api, "run_l2", spy_l2),
        patch.object(projects_api, "session_scope", _fake_session_scope),
        patch.object(handoff_api, "create_handoff_offer", fake_create_offer),
        patch.object(projects_api, "emit", fake_emit),
        patch.object(projects_api.cascade_tracker, "fail_run", spy_fail),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer_mode"] == "reserved_advice"
    assert body["refused"] is True
    assert body["handoff_id"] == str(handoff_id)
    assert body["disclaimer_id"]  # a disclaimer is attached
    assert "l2_ran" not in state  # the cascade was skipped entirely
    assert state.get("handoff_trigger") == "reserved_advice"
    assert state.get("failed_layer") == "reserved_advice"
    assert "analysis.run.refused" in state.get("events", [])  # type: ignore[arg-type]


def test_enforce_information_verdict_proceeds():
    """ENFORCE but a NON-reserved verdict (information): the gate must let the cascade run normally —
    only reserved_advice is refused, everything else flows through."""
    pid = uuid4()
    state: dict[str, object] = {}

    async def spy_classify(*, text, project_id, where, org_id=None):
        return _verdict("information")

    async def spy_l2(*_a, **_k):
        state["l2_ran"] = True
        return _fake_l2_report()

    async def fake_get_project(_pid):
        return SimpleNamespace(declared_intent="Jaké jsou mé GDPR povinnosti?")

    async def fake_emit(*_a, **_k):
        return None

    async def fake_health():
        return {}

    with (
        patch.object(projects_api, "get_settings", lambda: _settings(enabled=True, mode="enforce")),
        patch.object(projects_api, "get_project", fake_get_project),
        patch.object(projects_api, "classify_answer_mode", spy_classify),
        patch.object(projects_api, "run_l2", spy_l2),
        patch.object(projects_api, "emit", fake_emit),
        patch.object(projects_api.call_stats, "citation_health", fake_health),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 200, resp.text
    assert "answer_mode" not in resp.json()
    assert state.get("l2_ran") is True


# --------------------------------------------------------------------------- the OFF switches
def test_guard_disabled_skips_classification_entirely():
    """compliance_guard_enabled=False: the classifier is NEVER called (no spend, no audit) and the
    cascade runs — the gate is fully inert."""
    pid = uuid4()
    state: dict[str, object] = {}

    async def spy_classify(*, text, project_id, where):
        state["classified"] = True  # must NOT happen
        return _verdict("reserved_advice")

    async def spy_l2(*_a, **_k):
        state["l2_ran"] = True
        return _fake_l2_report()

    async def fake_emit(*_a, **_k):
        return None

    async def fake_health():
        return {}

    with (
        patch.object(
            projects_api, "get_settings", lambda: _settings(enabled=False, mode="enforce")
        ),
        patch.object(projects_api, "classify_answer_mode", spy_classify),
        patch.object(projects_api, "run_l2", spy_l2),
        patch.object(projects_api, "emit", fake_emit),
        patch.object(projects_api.call_stats, "citation_health", fake_health),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 200, resp.text
    assert "classified" not in state  # gate inert
    assert state.get("l2_ran") is True


def test_empty_declared_intent_skips_classification():
    """A project with no declared intent has nothing to classify: the gate skips the classifier (even
    in enforce) and proceeds — we never block on an empty string."""
    pid = uuid4()
    state: dict[str, object] = {}

    async def spy_classify(*, text, project_id, where):
        state["classified"] = True  # must NOT happen
        return _verdict("reserved_advice")

    async def spy_l2(*_a, **_k):
        state["l2_ran"] = True
        return _fake_l2_report()

    async def fake_get_project(_pid):
        return SimpleNamespace(declared_intent="   ")  # whitespace-only → treated as empty

    async def fake_emit(*_a, **_k):
        return None

    async def fake_health():
        return {}

    with (
        patch.object(projects_api, "get_settings", lambda: _settings(enabled=True, mode="enforce")),
        patch.object(projects_api, "get_project", fake_get_project),
        patch.object(projects_api, "classify_answer_mode", spy_classify),
        patch.object(projects_api, "run_l2", spy_l2),
        patch.object(projects_api, "emit", fake_emit),
        patch.object(projects_api.call_stats, "citation_health", fake_health),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 200, resp.text
    assert "classified" not in state
    assert state.get("l2_ran") is True
