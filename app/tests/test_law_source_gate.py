"""law_source='nexus' safety gate → HTTP 409 (UI Build Plan CP-6, Phase-B).

The danger this gate guards against: the full L2→L3→L4 cascade persists findings keyed by a
provision_id and then RE-RESOLVES those ids in L3/L4 against the app's OWN provision table. Under
law_source='nexus' the ids are NEXUS ids absent from that table, so the cascade would silently
yield empty/partial analysis with NO error — a lawyer would see a clean-looking but hollow result.
`require_cascade_law_source()` (regsuper/analysis/__init__.py) FAILS LOUD instead, and the analyze
endpoint maps that refusal to HTTP 409 (a clear config conflict, not a 500).

Two layers under test, both DB-free:
  1. the gate function itself — raises for any non-regsuper source, passes for regsuper, on L2/L3/L4.
  2. the 409 mapping — the /analyze route translates LawSourceNotSupported → 409 and records the refusal.

For (2) we mount ONLY the projects router (no app lifespan → no DB connect) and patch the layer
entrypoints + side-effects, so nothing touches Postgres.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper import analysis
from regsuper.analysis import LawSourceNotSupported, require_cascade_law_source
from regsuper.api import projects as projects_api


# --------------------------------------------------------------------------- the gate function
@pytest.mark.parametrize("layer", ["L2", "L3", "L4"])
def test_gate_passes_for_regsuper(layer):
    with patch.object(analysis, "get_settings", lambda: SimpleNamespace(law_source="regsuper")):
        # Must NOT raise — regsuper resolves end-to-end.
        require_cascade_law_source(layer)


@pytest.mark.parametrize("layer", ["L2", "L3", "L4"])
def test_gate_raises_for_nexus(layer):
    with patch.object(analysis, "get_settings", lambda: SimpleNamespace(law_source="nexus")):
        with pytest.raises(LawSourceNotSupported) as ei:
            require_cascade_law_source(layer)
    msg = str(ei.value)
    assert layer in msg
    assert "nexus" in msg
    assert "regsuper" in msg  # the message tells the operator how to fix it


def test_gate_raises_for_any_unknown_source():
    # Defense-in-depth: anything that isn't exactly 'regsuper' must be refused, not assumed safe.
    with patch.object(
        analysis, "get_settings", lambda: SimpleNamespace(law_source="something-else")
    ):
        with pytest.raises(LawSourceNotSupported):
            require_cascade_law_source("L2")


# --------------------------------------------------------------------------- B3-06 gate-relax (cutover_approved)
@pytest.mark.parametrize("layer", ["L2", "L3", "L4"])
def test_gate_still_refuses_nexus_when_cutover_not_approved(layer):
    """Default (cutover_approved=False) → nexus is still refused (byte-identical to pre-B3-06)."""
    with patch.object(
        analysis,
        "get_settings",
        lambda: SimpleNamespace(law_source="nexus", cutover_approved=False),
    ):
        with pytest.raises(LawSourceNotSupported):
            require_cascade_law_source(layer)


@pytest.mark.parametrize("layer", ["L2", "L3", "L4"])
def test_gate_admits_nexus_when_cutover_approved(layer):
    """B3-06: with the operator's explicit cutover_approved=True, the live nexus cascade is admitted."""
    with patch.object(
        analysis, "get_settings", lambda: SimpleNamespace(law_source="nexus", cutover_approved=True)
    ):
        require_cascade_law_source(layer)  # must NOT raise


def test_gate_regsuper_ignores_cutover_approved():
    """cutover_approved has no effect on the default regsuper path."""
    with patch.object(
        analysis,
        "get_settings",
        lambda: SimpleNamespace(law_source="regsuper", cutover_approved=True),
    ):
        require_cascade_law_source("L2")  # must NOT raise


# --------------------------------------------------------------------------- 409 HTTP mapping
def _client() -> TestClient:
    """A TestClient over JUST the projects router — no FastAPI lifespan, so no DB connect at import."""
    app = FastAPI()
    app.include_router(projects_api.router)
    return TestClient(app, raise_server_exceptions=True)


def test_analyze_returns_409_when_law_source_gated():
    """run_l2 raises LawSourceNotSupported (as it would when law_source='nexus') → the route returns 409
    with the refusal message, NOT a 500 and NOT a silent 200 with empty counts."""
    pid = uuid4()

    async def boom_l2(*_a, **_k):
        raise LawSourceNotSupported(
            "L2: law_source='nexus' is not wired for the full cascade. Set law_source='regsuper'."
        )

    async def fake_emit(*_a, **_k):
        return None

    with (
        patch.object(projects_api, "run_l2", boom_l2),
        patch.object(projects_api, "get_settings", lambda: SimpleNamespace(law_source="nexus")),
        patch.object(projects_api, "emit", fake_emit),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 409, resp.text
    assert "law_source" in resp.json()["detail"]
    assert "regsuper" in resp.json()["detail"]


def test_analyze_records_failed_run_on_gate():
    """The refusal must be VISIBLE: the cascade-tracker run is marked failed with failed_layer
    'law_source_gate' so an operator (or an audit) can see a bypass attempt was refused."""
    pid = uuid4()
    captured: dict[str, object] = {}

    async def boom_l2(*_a, **_k):
        raise LawSourceNotSupported("L2: nexus preview-only. Set law_source='regsuper'.")

    async def fake_emit(code, *_a, **_k):
        captured.setdefault("events", []).append(code)  # type: ignore[union-attr]

    real_fail = projects_api.cascade_tracker.fail_run

    def spy_fail(run, layer, error):
        captured["failed_layer"] = layer
        captured["fail_error"] = error
        return real_fail(run, layer, error)

    with (
        patch.object(projects_api, "run_l2", boom_l2),
        patch.object(projects_api, "get_settings", lambda: SimpleNamespace(law_source="nexus")),
        patch.object(projects_api, "emit", fake_emit),
        patch.object(projects_api.cascade_tracker, "fail_run", spy_fail),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 409
    assert captured.get("failed_layer") == "law_source_gate"
    assert "analysis.law_source.refused" in captured.get("events", [])  # type: ignore[arg-type]


def test_analyze_lookup_error_is_404_not_409():
    """Sanity guard so the two error paths don't get crossed: a missing project (LookupError) is a 404,
    distinct from the law_source 409. If someone reorders the except blocks, this fails."""
    pid = uuid4()

    async def missing_l2(*_a, **_k):
        raise LookupError(f"project {pid} not found")

    async def fake_emit(*_a, **_k):
        return None

    with (
        patch.object(projects_api, "run_l2", missing_l2),
        patch.object(projects_api, "get_settings", lambda: SimpleNamespace(law_source="regsuper")),
        patch.object(projects_api, "emit", fake_emit),
    ):
        resp = _client().post(f"/api/projects/{pid}/analyze", json={"layers": ["L2"]})

    assert resp.status_code == 404, resp.text
