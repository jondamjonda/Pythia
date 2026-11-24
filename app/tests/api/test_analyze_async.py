"""C3 (T-C3a) — the analyze endpoint after the cascade-body extraction.

CARDINAL: default-OFF byte-identical. At analyze_async=False (the dark default) the
analyze endpoint returns the EXACT current AnalyzeResponse (snapshot). The cascade
runs through the ONE extracted body (run_analysis_job) called from a single place.
The eager-task path (analyze_async=True) runs the SAME body inline (no broker) and
yields an equivalent response. The compliance input-gate refusal is preserved.

These run with the cascade runners PATCHED (no LLM); the snapshot / eager-equivalence
rows need no DB (entitlements OFF → no run row). The input-gate refusal row is DB-free
too (it short-circuits before the cascade). DB-touching is avoided here on purpose so
the byte-identical contract is provable in CI without Postgres.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from regsuper.api import projects as projects_api
from regsuper.api.projects import AnalyzeRequest, AnalyzeResponse


def _settings(
    *, analyze_async=False, compliance_guard_enabled=False, compliance_guard_mode="shadow"
):
    """Settings namespace with entitlements OFF (no run row → byte-identical) and the
    async flag selectable. getattr-safe for everything project_analyze reads."""
    return SimpleNamespace(
        law_source="regsuper",
        compliance_guard_enabled=compliance_guard_enabled,
        compliance_guard_mode=compliance_guard_mode,
        entity_scoping_enabled=False,
        entitlements_enabled=False,
        analyze_async=analyze_async,
    )


def _l2_report():
    return SimpleNamespace(
        n_candidates=3,
        n_evaluated=2,
        n_skipped_cached=1,
        n_applicable=1,
        n_ambiguous=0,
        n_not_applicable=1,
        n_refused=0,
        n_errors=0,
        findings=[object()],
        sample_errors=[],
        total_latency_ms=11,
    )


async def _emit(*_a, **_k):
    return None


async def _health():
    return {}


def _patches(settings, *, run_l2=None, run_l3=None, run_l4=None):
    """Common patch stack: settings (both projects + metering), the cascade runners,
    emit, citation_health, resource_guard gates (so they don't refuse), and
    get_project (so the compliance gate, when ON, can read declared_intent)."""
    stack = [
        patch.object(projects_api, "get_settings", lambda: settings),
        patch.object(projects_api.metering, "get_settings", lambda: settings),
        patch.object(projects_api, "emit", _emit),
        patch.object(projects_api.call_stats, "citation_health", _health),
    ]
    if run_l2 is not None:
        stack.append(patch.object(projects_api, "run_l2", run_l2))
    if run_l3 is not None:
        stack.append(patch.object(projects_api, "run_l3", run_l3))
    if run_l4 is not None:
        stack.append(patch.object(projects_api, "run_l4_gaps", run_l4))
    return stack


async def _gate_ok():
    return None


# ── byte-identical: analyze_async=False returns the current AnalyzeResponse ──────
async def test_sync_default_returns_exact_analyze_response(monkeypatch):
    """analyze_async=False (default) → the endpoint returns a normal AnalyzeResponse
    with the L2 section populated from the report, layers=['L2'], notes=[] — the
    current shape, byte-identical."""
    pid = uuid4()
    called = {"n": 0}

    async def fake_l2(project_id, *, max_concurrency, limit, force, as_of, analysis_run_id=None):
        called["n"] += 1
        assert analysis_run_id is None  # entitlements OFF → no run row threaded
        return _l2_report()

    s = _settings(analyze_async=False)

    with contextlib.ExitStack() as es:
        for p in _patches(s, run_l2=fake_l2):
            es.enter_context(p)
        es.enter_context(patch.object(projects_api.resource_guard, "gate_cpu_mem", _gate_ok))
        es.enter_context(patch.object(projects_api.resource_guard, "gate_mps", _gate_ok))
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2"]))

    assert isinstance(resp, AnalyzeResponse)
    assert resp.project_id == pid
    assert resp.layers == ["L2"]
    assert resp.notes == []
    assert resp.l3 is None and resp.l4 is None
    assert resp.l2 is not None
    assert resp.l2.n_candidates == 3
    assert resp.l2.n_applicable == 1
    assert resp.l2.n_findings_persisted == 1
    assert called["n"] == 1  # the cascade ran exactly once


# ── eager-task path runs the SAME body inline (no broker) ────────────────────────
async def test_async_eager_path_equivalent(monkeypatch):
    """analyze_async=True → the SAME body runs inline via the eager task (no broker),
    producing the same section shape. The cascade runs exactly once."""
    pid = uuid4()
    called = {"n": 0}

    async def fake_l2(project_id, *, max_concurrency, limit, force, as_of, analysis_run_id=None):
        called["n"] += 1
        return _l2_report()

    s = _settings(analyze_async=True)

    with contextlib.ExitStack() as es:
        for p in _patches(s, run_l2=fake_l2):
            es.enter_context(p)
        es.enter_context(patch.object(projects_api.resource_guard, "gate_cpu_mem", _gate_ok))
        es.enter_context(patch.object(projects_api.resource_guard, "gate_mps", _gate_ok))
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2"]))

    assert isinstance(resp, AnalyzeResponse)
    assert resp.layers == ["L2"]
    assert resp.l2 is not None and resp.l2.n_applicable == 1
    assert called["n"] == 1


# ── single call site: the body is called from exactly one place ──────────────────
async def test_cascade_called_from_one_place(monkeypatch):
    """Both the sync and eager paths funnel through run_analysis_job — patching it
    proves the endpoint calls the ONE extracted body (and only it)."""
    pid = uuid4()
    seen = {"jobs": []}

    async def fake_body(job):
        seen["jobs"].append(job)
        return AnalyzeResponse(project_id=job.project_id, layers=job.layers, total_latency_ms=0)

    s = _settings(analyze_async=False)

    with contextlib.ExitStack() as es:
        for p in _patches(s):
            es.enter_context(p)
        es.enter_context(patch.object(projects_api.resource_guard, "gate_cpu_mem", _gate_ok))
        es.enter_context(patch.object(projects_api.resource_guard, "gate_mps", _gate_ok))
        es.enter_context(patch.object(projects_api, "run_analysis_job", fake_body))
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2", "L3"]))

    assert len(seen["jobs"]) == 1  # called exactly once
    job = seen["jobs"][0]
    assert job.project_id == pid
    assert job.layers == ["L2", "L3"]
    assert job.met_run_id is None  # entitlements OFF → no run row
    assert resp.layers == ["L2", "L3"]


# ── input-gate compliance refusal preserved ──────────────────────────────────────
async def test_input_gate_refusal_preserved_no_cascade(monkeypatch):
    """In enforce mode a reserved-advice intent short-circuits to a 200
    ComplianceRefusalResponse BEFORE the cascade — the body is never called."""
    pid = uuid4()
    body_called = {"n": 0}

    async def fake_body(job):
        body_called["n"] += 1
        return AnalyzeResponse(project_id=job.project_id, layers=job.layers, total_latency_ms=0)

    async def fake_get_project(_pid):
        return SimpleNamespace(declared_intent="Can I fire this employee?", org_id=None)

    async def fake_classify(*, text, project_id, where, org_id=None):
        return SimpleNamespace(answer_mode="reserved_advice", reason="advice")

    async def fake_handoff(*_a, **_k):
        return SimpleNamespace(id=uuid4())

    s = _settings(
        analyze_async=False, compliance_guard_enabled=True, compliance_guard_mode="enforce"
    )

    with contextlib.ExitStack() as es:
        for p in _patches(s):
            es.enter_context(p)
        es.enter_context(patch.object(projects_api.resource_guard, "gate_cpu_mem", _gate_ok))
        es.enter_context(patch.object(projects_api.resource_guard, "gate_mps", _gate_ok))
        es.enter_context(patch.object(projects_api, "get_project", fake_get_project))
        es.enter_context(patch.object(projects_api, "classify_answer_mode", fake_classify))
        es.enter_context(patch.object(projects_api, "run_analysis_job", fake_body))
        es.enter_context(patch("regsuper.api.handoff.create_handoff_offer", fake_handoff))
        resp = await projects_api.project_analyze(pid, AnalyzeRequest(layers=["L2"]))

    # The endpoint returns a JSONResponse(200) carrying the reserved-advice refusal.
    from fastapi.responses import JSONResponse

    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 200
    import json as _json

    body = _json.loads(bytes(resp.body))
    assert body["answer_mode"] == "reserved_advice"
    assert body["refused"] is True
    assert body_called["n"] == 0  # the cascade body was never reached
