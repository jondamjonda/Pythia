"""VL-G-04 (WS3 step 29) — the engine→app claim-verify summary bridge.

Pins: a loop-ON answer (ledger summary present in audit_trace) emits ONE answer.claims.summary event carrying
COUNTS ONLY (WP-029 — no query/claim/provision text); a loop-OFF answer (no ledger) emits nothing.
"""

from __future__ import annotations

import pytest

from regsuper.api import answer as ans_mod


@pytest.mark.asyncio
async def test_emits_summary_counts_from_ledger(monkeypatch):
    emitted: list[tuple] = []

    async def _spy(code, **kw):
        emitted.append((code, kw))

    monkeypatch.setattr(ans_mod.events, "emit", _spy)
    ans = {
        "drafted_answer": "Depozitář musí…",
        "audit_trace": {
            "claim_ledger": {
                "summary": {
                    "mode": "enforce",
                    "claims_total": 5,
                    "demoted": 2,
                    "per_status": {"verified": 2, "dropped_safety": 1},
                    "claim_verify_ms": 123.4,
                }
            }
        },
    }
    await ans_mod._emit_claims_summary(ans, "ORG-1")

    assert len(emitted) == 1
    code, kw = emitted[0]
    assert code == "answer.claims.summary"
    assert (
        kw["claims_total"] == 5
        and kw["demoted"] == 2
        and kw["dropped"] == 1
        and kw["mode"] == "enforce"
    )
    # WP-029: counts only — no draft/claim/provision text anywhere in the payload.
    blob = repr(kw)
    assert "Depozitář" not in blob and "drafted_answer" not in blob


@pytest.mark.asyncio
async def test_no_ledger_emits_nothing(monkeypatch):
    emitted: list = []

    async def _spy(code, **kw):
        emitted.append(code)

    monkeypatch.setattr(ans_mod.events, "emit", _spy)
    await ans_mod._emit_claims_summary(
        {"drafted_answer": "x"}, "ORG-1"
    )  # loop OFF ⇒ no claim_ledger
    assert emitted == []
