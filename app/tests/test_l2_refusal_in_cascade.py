"""B2a-05 / B2b-03 (WP-038) — L2 below-floor refusal-in-cascade (G7).

When the L2 applicability LLM judges a provision applicable (True / 'ambiguous') but its confidence falls
BELOW the calibrated cascade applicability floor, `run_l2` must NOT publish a confident
`finding_type='applicable'` row (the false-positive mode) and must NOT silently drop it (the silent-miss
mode). Instead it records a durable, CITED refusal so the answer is honest and reviewable.

Safety invariants pinned here (the task's a/b/c):

  (a) applicable=True + confidence 0.30 + floor 0.50 →
        * exactly ONE finding routed through `_persist_refusal` (NOT `_persist_finding`)
        * the persisted Finding is finding_type='refusal', answer_mode='needs_review',
          severity != 'applicable', cites the SAME provision (a single-element cited_provisions)
        * the real citation hook (`_enforce_citation_on_finding`) accepts it — so the
          `cited_provisions_nonempty` CHECK + the Python-side EmptyCitationError both still hold
          (a refusal is never an empty-citation row)
        * report.n_refused == 1
        * the already-registered `analysis.applicability.refused` event is emitted with confidence + floor

  (b) ABOVE-floor BYTE-UNCHANGED — an applicable=True + confidence 0.90 verdict (floor 0.50) goes through
        `_persist_finding`, NEVER `_persist_refusal`; no refusal event; report.n_refused == 0.

  (c) L3 EXCLUDES the refusal — the `_l2_applicable_provisions` SQL filter selects
        finding_type='applicable' AND severity='applicable'; a finding_type='refusal' (severity
        'needs_review') row therefore never re-enters the obligation lane. We pin the filter text AND
        prove the refusal row our persist builds would not match it.

Fully mocked — no DB, no network, no OpenAI. We patch the orchestration's DB/LLM seams (mirroring
test_cascade_killswitch_abort.py) and capture the Finding object handed to `session.add` so we can assert
on the real ORM row and run the real citation hook against it.

asyncio_mode=auto (pyproject) → async test functions run directly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from regsuper.analysis import l2_applicability as l2
from regsuper.analysis.l1_candidates import L1Candidate
from regsuper.storage.audit import EmptyCitationError, _enforce_citation_on_finding
from regsuper.storage.models import Finding


# --------------------------------------------------------------------------- shared fakes
def _candidate() -> L1Candidate:
    return L1Candidate(
        provision_id=uuid4(),
        label="§1",
        legal_hierarchy="zakon",
        jurisdiction="CZ",
        source_external_id="ext-1",
        source_title="Act",
        priority_weight=1,
    )


class _CaptureSession:
    """A fake AsyncSession that records every Finding added and runs the REAL citation hook on flush,
    so the test proves the persisted refusal satisfies the cited_provisions_nonempty CHECK / EmptyCitationError
    exactly as the live DB insert would."""

    def __init__(self, added: list[Finding], doc_rows: list[tuple]):
        self._added = added
        self._doc_rows = doc_rows

    async def execute(self, *_a, **_k):
        rows = self._doc_rows

        class _Result:
            def all(self_inner):
                return rows

            def first(self_inner):
                return rows[0] if rows else None

        return _Result()

    def add(self, obj):
        if isinstance(obj, Finding):
            # Mirror the live before_insert hook: an empty-citation finding would raise here.
            _enforce_citation_on_finding(None, None, obj)
            self._added.append(obj)

    async def flush(self):
        return None


def _make_session_scope(added: list[Finding], doc_rows: list[tuple]):
    @asynccontextmanager
    async def _scope():
        yield _CaptureSession(added, doc_rows)

    return _scope


async def _fake_get_project(_pid):
    return SimpleNamespace(id=uuid4(), org_id=uuid4())


def _patch_common(module, added: list[Finding], doc_rows: list[tuple], events: list[tuple]):
    async def _capture_emit(name, **kw):
        events.append((name, kw))

    return [
        patch.object(module, "require_cascade_law_source", lambda layer: None),
        patch.object(module, "get_project", _fake_get_project),
        patch.object(module, "session_scope", _make_session_scope(added, doc_rows)),
        patch.object(module, "emit", _capture_emit),
        patch.object(
            module,
            "get_settings",
            lambda: SimpleNamespace(openai_api_key="sk-test", law_source="regsuper"),
        ),
        patch.object(module, "AsyncOpenAI", lambda **_k: object()),
        patch.object(module, "_existing_l2_provisions", _fake_empty_set),
        # Audit row write is exercised separately; stub it so the capture session only sees Findings.
        patch.object(module, "_audit_l2_call", _fake_noop),
    ]


async def _fake_empty_set(_pid):
    return set()


async def _fake_noop(**_k):
    return None


class _enter_all:
    def __init__(self, ctxs):
        self._ctxs = ctxs

    def __enter__(self):
        for c in self._ctxs:
            c.__enter__()
        return self

    def __exit__(self, *exc):
        for c in reversed(self._ctxs):
            c.__exit__(*exc)
        return False


# --------------------------------------------------------------------------- (a) below-floor → cited refusal
async def test_below_floor_writes_a_cited_refusal_with_needs_review_and_emits_event():
    cand = _candidate()
    sdid = uuid4()
    added: list[Finding] = []
    events: list[tuple] = []
    doc_rows = [(cand.provision_id, sdid)]

    persist_finding_calls: list = []

    async def fake_evaluate(*, client, project, provision_id, model, analysis_run_id=None):
        return l2.ApplicabilityCall(
            provision_id=provision_id,
            label="§1",
            source_external_id="ext-1",
            result=l2.ApplicabilityResult(
                applicable=True,
                confidence=0.30,
                applicable_text_span="span",
                reasoning="reason",
            ),
            error=None,
            latency_ms=1,
            provision_text="provision text",
        )

    async def spy_persist_finding(**kw):  # MUST NOT be called for a below-floor verdict
        persist_finding_calls.append(kw)
        return uuid4()

    ctx = _patch_common(l2, added, doc_rows, events)
    ctx += [
        patch.object(l2, "evaluate_provision", fake_evaluate),
        patch.object(l2, "_persist_finding", spy_persist_finding),
        patch.object(
            l2,
            "get_cascade_refusal_floor",
            lambda: SimpleNamespace(value=0.50, source="fallback"),
        ),
    ]

    with _enter_all(ctx):
        report = await l2.run_l2(uuid4(), candidates=[cand], max_concurrency=1)

    # The above-floor finding path was NOT taken.
    assert persist_finding_calls == [], "below-floor verdict must NOT call _persist_finding"

    # Exactly one Finding persisted, and it is a CITED refusal.
    assert len(added) == 1, "exactly one refusal finding should be persisted"
    f = added[0]
    assert f.finding_type == "refusal"
    assert f.answer_mode == "needs_review"
    assert f.analysis_layer == "L2"
    assert f.severity != "applicable", (
        "refusal severity must NOT be 'applicable' (so L3 excludes it)"
    )
    # CITED — single-element cited_provisions pointing at the SAME provision (CHECK + EmptyCitationError hold;
    # the capture session already ran the real hook on add(), so reaching here proves it did not raise).
    assert f.cited_provisions == [cand.provision_id]
    assert f.cited_documents == [sdid]

    # n_refused counter + the report shape.
    assert report.n_refused == 1
    assert report.n_applicable == 1  # the raw verdict tally is still applicable=True
    assert len(report.findings) == 1

    # The already-registered refusal event fired with confidence + floor.
    refused = [kw for (name, kw) in events if name == "analysis.applicability.refused"]
    assert len(refused) == 1, "exactly one analysis.applicability.refused event"
    assert refused[0]["confidence"] == 0.30
    assert refused[0]["floor"] == 0.50


async def test_refusal_finding_is_not_an_empty_citation_row():
    """Directly assert the invariant the task calls out: the refusal CITES the provision, so the
    EmptyCitationError guard (and thus the cited_provisions_nonempty CHECK) is satisfied — and would FIRE if
    the citation were ever dropped."""
    cand = _candidate()
    sdid = uuid4()
    added: list[Finding] = []
    events: list[tuple] = []
    doc_rows = [(cand.provision_id, sdid)]

    async def fake_evaluate(*, client, project, provision_id, model, analysis_run_id=None):
        return l2.ApplicabilityCall(
            provision_id=provision_id,
            label="§1",
            source_external_id="ext-1",
            result=l2.ApplicabilityResult(applicable="ambiguous", confidence=0.10),
            error=None,
            latency_ms=1,
            provision_text="text",
        )

    ctx = _patch_common(l2, added, doc_rows, events)
    ctx += [
        patch.object(l2, "evaluate_provision", fake_evaluate),
        patch.object(
            l2,
            "get_cascade_refusal_floor",
            lambda: SimpleNamespace(value=0.50, source="fallback"),
        ),
    ]
    with _enter_all(ctx):
        report = await l2.run_l2(uuid4(), candidates=[cand], max_concurrency=1)

    assert report.n_refused == 1
    f = added[0]
    assert f.cited_provisions, "a refusal must still cite — empty would be an EmptyCitationError"
    # Belt-and-braces: the real guard accepts our refusal but rejects an empty one.
    _enforce_citation_on_finding(None, None, f)  # no raise
    f.cited_provisions = []
    with pytest.raises(EmptyCitationError):
        _enforce_citation_on_finding(None, None, f)


# --------------------------------------------------------------------------- (b) above-floor BYTE-UNCHANGED
async def test_above_floor_takes_the_unchanged_persist_finding_path():
    cand = _candidate()
    sdid = uuid4()
    added: list[Finding] = []
    events: list[tuple] = []
    doc_rows = [(cand.provision_id, sdid)]

    persist_finding_calls: list = []
    persist_refusal_calls: list = []

    async def fake_evaluate(*, client, project, provision_id, model, analysis_run_id=None):
        return l2.ApplicabilityCall(
            provision_id=provision_id,
            label="§1",
            source_external_id="ext-1",
            result=l2.ApplicabilityResult(
                applicable=True,
                confidence=0.90,
                applicable_text_span="span",
                reasoning="reason",
            ),
            error=None,
            latency_ms=1,
            provision_text="text",
        )

    async def spy_persist_finding(**kw):
        persist_finding_calls.append(kw)
        return uuid4()

    async def spy_persist_refusal(**kw):  # MUST NOT be called above the floor
        persist_refusal_calls.append(kw)
        return uuid4()

    ctx = _patch_common(l2, added, doc_rows, events)
    ctx += [
        patch.object(l2, "evaluate_provision", fake_evaluate),
        patch.object(l2, "_persist_finding", spy_persist_finding),
        patch.object(l2, "_persist_refusal", spy_persist_refusal),
        patch.object(
            l2,
            "get_cascade_refusal_floor",
            lambda: SimpleNamespace(value=0.50, source="fallback"),
        ),
    ]
    with _enter_all(ctx):
        report = await l2.run_l2(uuid4(), candidates=[cand], max_concurrency=1)

    assert len(persist_finding_calls) == 1, (
        "above-floor must take the byte-unchanged _persist_finding path"
    )
    assert persist_refusal_calls == [], "above-floor must NOT route to _persist_refusal"
    assert report.n_refused == 0
    assert report.n_applicable == 1
    assert not [e for e in events if e[0] == "analysis.applicability.refused"]


# --------------------------------------------------------------------------- (c) L3 excludes the refusal
def test_l3_applicable_filter_excludes_refusals_by_type_and_severity():
    """L3's `_l2_applicable_provisions` selects ONLY finding_type='applicable' AND severity='applicable'.
    A refusal row (finding_type='refusal', severity='needs_review') matches neither predicate, so L3 can
    never double-process it. We pin the filter text (regression) and assert a refusal row's fields fail it."""
    import inspect

    from regsuper.analysis import l3_obligations as l3

    src = inspect.getsource(l3._l2_applicable_provisions)
    assert "finding_type = 'applicable'" in src
    assert "severity = 'applicable'" in src

    # Build a refusal exactly as _persist_refusal would, and confirm it fails BOTH predicates.
    refusal = Finding(
        project_id=uuid4(),
        finding_type="refusal",
        statement="uncertain",
        severity="needs_review",
        cited_provisions=[uuid4()],
        cited_documents=[uuid4()],
        confidence_overall=0.30,
        analysis_layer="L2",
        generated_by="gpt-4o-mini",
        answer_mode="needs_review",
    )
    assert refusal.finding_type != "applicable"
    assert refusal.severity != "applicable"
