"""B3-05 — L2 resolves the app-side cited document for a NEXUS provision via the crosswalk (the cutover).

The parallel of B3-04 for L2. Under law_source='nexus' the L1/L2 work ids are NEXUS ids absent from the app
Provision table, so L2's normal source_document lookup finds nothing and every finding is dropped for a
missing source_document_id (why the nexus cascade is preview-only today). `_nexus_doc_lookup` resolves each
nexus_id → regsuper_id → app source_document_id via provision_id_map (pinned to nexus_corpus), so the SAME
act's app document can be cited. SHIPS DARK: law_source defaults to 'regsuper' (the helper is never called
and the app-table doc_lookup is byte-unchanged), and run_l2's require_cascade_law_source('L2') gate still
refuses nexus end-to-end. A nexus id with no crosswalk row is simply absent → that finding is dropped for a
missing source_document_id, exactly as before (honest under-coverage, never a guessed document).

All DB-free: session_scope is stubbed. (asyncio_mode=auto → async tests run directly.)
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from regsuper.analysis import l2_applicability
from regsuper.analysis.l1_candidates import L1Candidate
from regsuper.storage.audit import _enforce_citation_on_finding
from regsuper.storage.models import Finding

N1, N2, N3 = uuid4(), uuid4(), uuid4()  # nexus ids
S1, S2 = uuid4(), uuid4()  # app source_document ids


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


def _fake_scope(rows):
    @contextlib.asynccontextmanager
    async def _cm():
        yield _FakeSession(rows)

    return _cm()


def _boom_scope():
    raise AssertionError("session_scope must not be entered for an empty id batch")


async def test_nexus_doc_lookup_maps_batch():
    """Each crosswalk row (nexus_id, app source_document_id) becomes a doc_lookup entry."""
    with patch.object(l2_applicability, "session_scope", lambda: _fake_scope([(N1, S1), (N2, S2)])):
        out = await l2_applicability._nexus_doc_lookup([N1, N2])
    assert out == {N1: S1, N2: S2}


async def test_nexus_doc_lookup_partial_miss_leaves_unmapped_id_absent():
    """A nexus id with no crosswalk row is ABSENT from the result (not None, not guessed) — run_l2 then
    drops that finding for a missing source_document_id, exactly as the regsuper path would."""
    with patch.object(l2_applicability, "session_scope", lambda: _fake_scope([(N1, S1)])):
        out = await l2_applicability._nexus_doc_lookup([N1, N3])
    assert out == {N1: S1}
    assert N3 not in out


async def test_nexus_doc_lookup_empty_batch_does_no_query():
    """An empty work batch short-circuits before touching the DB."""
    with patch.object(l2_applicability, "session_scope", _boom_scope):
        assert await l2_applicability._nexus_doc_lookup([]) == {}


# --------------------------------------------------------------------------- run_l2 nexus branch (the wiring)
# A compact mirror of test_l2_refusal_in_cascade.py's harness, but driving the NEXUS branch: the gate is
# stubbed to a no-op (so run_l2's body runs), get_settings reports law_source='nexus' (so the doc_lookup is
# built from _nexus_doc_lookup, which we patch), and a CaptureSession records the persisted Finding so we can
# assert its cited_documents is the crosswalk-resolved app document — proving the branch end-to-end, not just
# the helper in isolation.
class _CaptureSession:
    def __init__(self, added: list[Finding]):
        self._added = added

    async def execute(self, *_a, **_k):
        class _R:
            def all(self_inner):
                return []

            def first(self_inner):
                return None

        return _R()

    def add(self, obj):
        if isinstance(obj, Finding):
            _enforce_citation_on_finding(
                None, None, obj
            )  # real before_insert hook (empty-citation guard)
            self._added.append(obj)

    async def flush(self):
        return None


def _capture_scope(added: list[Finding]):
    @asynccontextmanager
    async def _scope():
        yield _CaptureSession(added)

    return _scope


async def _fake_get_project(_pid):
    return SimpleNamespace(id=uuid4(), org_id=uuid4())


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


def _nexus_ctx(added: list[Finding], events: list[tuple]):
    async def _emit(name, **kw):
        events.append((name, kw))

    return [
        patch.object(
            l2_applicability, "require_cascade_law_source", lambda layer: None
        ),  # gate stubbed → body runs
        patch.object(l2_applicability, "get_project", _fake_get_project),
        patch.object(l2_applicability, "session_scope", _capture_scope(added)),
        patch.object(l2_applicability, "emit", _emit),
        patch.object(
            l2_applicability,
            "get_settings",
            lambda: SimpleNamespace(openai_api_key="sk-test", law_source="nexus"),
        ),
        patch.object(l2_applicability, "AsyncOpenAI", lambda **_k: object()),
        patch.object(l2_applicability, "_existing_l2_provisions", _fake_empty_set),
        patch.object(l2_applicability, "_audit_l2_call", _fake_noop),
        patch.object(
            l2_applicability,
            "get_cascade_refusal_floor",
            lambda: SimpleNamespace(value=0.50, source="fallback"),
        ),
    ]


def _nexus_candidate() -> L1Candidate:
    return L1Candidate(
        provision_id=uuid4(),
        label="§1",
        legal_hierarchy="zakon",
        jurisdiction="CZ",
        source_external_id="ext-1",
        source_title="Act",
        priority_weight=1,
    )


async def _applicable_evaluate_factory(provision_text: str = "provision text"):
    async def fake_evaluate(*, client, project, provision_id, model, analysis_run_id=None):
        return l2_applicability.ApplicabilityCall(
            provision_id=provision_id,
            label="§1",
            source_external_id="ext-1",
            result=l2_applicability.ApplicabilityResult(
                applicable=True,
                confidence=0.90,  # above the 0.50 floor → the persist-finding path
                applicable_text_span="span",
                reasoning="reason",
            ),
            error=None,
            latency_ms=1,
            provision_text=provision_text,
        )

    return fake_evaluate


async def test_run_l2_persists_nexus_finding_with_crosswalk_document():
    """End-to-end: under law_source='nexus', run_l2 builds doc_lookup from _nexus_doc_lookup and the persisted
    L2 finding cites the crosswalk-resolved APP document (cited_documents=[sdid]) — proving the branch wiring,
    not just the helper."""
    cand = _nexus_candidate()
    sdid = uuid4()
    added: list[Finding] = []
    events: list[tuple] = []

    async def fake_nexus_doc_lookup(ids):
        assert ids == [cand.provision_id]  # built from the work ids, via the crosswalk
        return {cand.provision_id: sdid}

    ctx = [
        *_nexus_ctx(added, events),
        patch.object(l2_applicability, "evaluate_provision", await _applicable_evaluate_factory()),
        patch.object(l2_applicability, "_nexus_doc_lookup", fake_nexus_doc_lookup),
    ]
    with _enter_all(ctx):
        report = await l2_applicability.run_l2(uuid4(), candidates=[cand], max_concurrency=1)

    assert len(added) == 1, "the applicable nexus finding should persist"
    f = added[0]
    assert f.cited_provisions == [cand.provision_id]
    assert f.cited_documents == [sdid]  # the crosswalk-resolved app document
    assert report.n_applicable == 1


async def test_run_l2_drops_nexus_finding_when_crosswalk_misses():
    """A nexus provision with no crosswalk row → _nexus_doc_lookup omits it → run_l2 drops the finding for a
    missing source_document_id (no persist, logged) — the same honest under-coverage as the regsuper path,
    never a guessed document."""
    cand = _nexus_candidate()
    added: list[Finding] = []
    events: list[tuple] = []

    async def empty_lookup(_ids):
        return {}  # crosswalk miss

    ctx = [
        *_nexus_ctx(added, events),
        patch.object(l2_applicability, "evaluate_provision", await _applicable_evaluate_factory()),
        patch.object(l2_applicability, "_nexus_doc_lookup", empty_lookup),
    ]
    with _enter_all(ctx):
        report = await l2_applicability.run_l2(uuid4(), candidates=[cand], max_concurrency=1)

    assert added == [], "no finding may persist without a resolvable source document"
    assert any("missing source_document_id" in str(e) for e in report.sample_errors)
