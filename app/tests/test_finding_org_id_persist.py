"""Regression: every cascade persist path stamps finding.org_id from the OWNING PROJECT.

`finding.org_id` is NOT NULL since migration 007 (findings inherit the tenant of their project). The L2/L3/L4
persist helpers did not set it, so any finding insert against a 007+ schema failed with a NotNullViolation —
masked in prod only because the live DB is the pre-007 frozen snapshot, but fatal for a fresh DB and for the
B3-05 off-flip eval (it silently dropped EVERY applicable verdict at persist, looking like under-coverage).

These pin that the L2 finding + refusal persists stamp the passed org_id. (L3 obligation + L4 gap persists are
covered by test_b3_optA_corpus_resolver's org_id assertions.) DB-free: session_scope is captured in memory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from regsuper.analysis import l2_applicability as l2


def _captured_scope(captured: list):
    @asynccontextmanager
    async def _cap():
        class _S:
            def add(self, obj):
                captured.append(obj)

            async def flush(self):
                return None

        yield _S()

    return _cap


def _call(confidence: float) -> l2.ApplicabilityCall:
    res = l2.ApplicabilityResult(
        applicable=True, confidence=confidence, applicable_text_span="span", reasoning="because"
    )
    return l2.ApplicabilityCall(
        provision_id=uuid4(),
        label="§",
        source_external_id="ext",
        result=res,
        error=None,
        latency_ms=1,
        provision_text="span text here",
    )


async def test_persist_finding_stamps_org_id():
    org = uuid4()
    captured: list = []
    with (
        patch.object(l2, "session_scope", _captured_scope(captured)),
        patch.object(l2, "get_settings", lambda: SimpleNamespace(law_source="regsuper")),
    ):
        await l2._persist_finding(
            project_id=uuid4(),
            org_id=org,
            provision_id=uuid4(),
            source_document_id=uuid4(),
            call=_call(0.9),
            provision_text="span text here",
            model="m",
        )
    assert captured[0].org_id == org


async def test_persist_refusal_stamps_org_id():
    org = uuid4()
    captured: list = []
    with (
        patch.object(l2, "session_scope", _captured_scope(captured)),
        patch.object(l2, "get_settings", lambda: SimpleNamespace(law_source="regsuper")),
    ):
        await l2._persist_refusal(
            project_id=uuid4(),
            org_id=org,
            provision_id=uuid4(),
            source_document_id=uuid4(),
            call=_call(0.2),
            floor=0.5,
            model="m",
        )
    assert captured[0].org_id == org
