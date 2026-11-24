"""VL-G-08 (WS3 step 27, app half) — the engine request id lands on the costed pythia transport row.

The app reads the rid from the engine answer's audit_trace and stamps it on the api_call row's
response_summary, so a Polarius cost row joins to the exact engine trace. DB-free (spies the persist).
"""

from __future__ import annotations

import pytest

from regsuper.pythia import client as pc


@pytest.mark.asyncio
async def test_track_stamps_rid_in_response_summary(monkeypatch):
    persisted: list[dict] = []

    async def _spy_persist(**kwargs):
        persisted.append(kwargs)

    async def _noop(*a, **k):
        return None

    import regsuper.observability.api_tracker as _api_tracker

    monkeypatch.setattr(_api_tracker, "_persist_row", _spy_persist)
    monkeypatch.setattr(pc, "_write_engine_llm_rows", _noop)
    monkeypatch.setattr(pc, "emit", _noop, raising=False)
    import regsuper.observability.events as _events

    monkeypatch.setattr(_events, "emit", _noop)

    async def _fn():
        return {"drafted_answer": "x", "audit_trace": {"request_id": "req-000042"}}

    out = await pc._track("answer", _fn)
    assert out["drafted_answer"] == "x"
    transport = next(r for r in persisted if r.get("source") == "pythia.proxy")
    assert transport["response_summary"] == "rid=req-000042"


@pytest.mark.asyncio
async def test_track_no_rid_keeps_summary_none(monkeypatch):
    persisted: list[dict] = []

    async def _spy_persist(**kwargs):
        persisted.append(kwargs)

    async def _noop(*a, **k):
        return None

    import regsuper.observability.api_tracker as _api_tracker
    import regsuper.observability.events as _events

    monkeypatch.setattr(_api_tracker, "_persist_row", _spy_persist)
    monkeypatch.setattr(pc, "_write_engine_llm_rows", _noop)
    monkeypatch.setattr(_events, "emit", _noop)

    async def _fn():
        return {"drafted_answer": "x"}  # no audit_trace ⇒ no rid

    await pc._track("answer", _fn)
    transport = next(r for r in persisted if r.get("source") == "pythia.proxy")
    assert transport["response_summary"] is None
