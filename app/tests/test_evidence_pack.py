"""Reproducible EVIDENCE PACK (gated-decisions prep) — additive, default-OFF, READ-ONLY.

Three DB-FREE layers:

1. PURE determinism — ``build_evidence_pack`` on the same rows yields a byte-identical content dict AND an
   identical SHA-256 (the tamper-evidence / reproducibility keystone); the embedded ``analysis_export`` hashes
   byte-identically to a standalone audit export (cross-checkable). The temporal / refusal proofs are exact.
2. GATED-OFF surface — mounting ONLY this router on a bare FastAPI app with ``audit_evidence_enabled=False``
   makes the route 404 (inert default), exactly like the audit-export gate.
3. HAPPY-PATH route (DB mocked) — with the flag ON and a fake session, a pack carries a non-null content_hash,
   the correct verdict flags, and is reproducible across two calls.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import evidence as evidence_api
from regsuper.api import export as export_api
from regsuper.storage.models import AnalysisRun, AuditEvent, Finding, Project, Provision


# ── settings double (mirror test_audit_export._settings) ─────────────────────
def _settings(**over):
    base = dict(
        audit_evidence_enabled=False,
        audit_export_enabled=False,
        export_pdf_enabled=False,
        data_residency_compute_region="EU",
        data_residency_storage_region="EU",
        openai_dpa_url="https://openai.com/dpa",
        law_source="regsuper",
        embedding_model="text-embedding-3-large",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── row doubles ──────────────────────────────────────────────────────────────
def _prov(pid, *, valid_from=None, valid_to=None, status="in-force", tier="hard-law"):
    return SimpleNamespace(
        id=pid,
        label="§ 5",
        eid="e5",
        hierarchy_path="a.b",
        jurisdiction="CZ",
        legal_hierarchy="zakon",
        text="provision text",
        bindingness_tier=tier,
        temporal_status=status,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def _finding(fid, pid, *, finding_type="obligation", answer_mode="information", docs=("d1",)):
    return SimpleNamespace(
        id=fid,
        project_id=uuid4(),
        finding_type=finding_type,
        statement="S",
        severity="high",
        confidence_overall=0.7,
        review_state="unreviewed",
        answer_mode=answer_mode,
        cited_provisions=[pid],
        cited_documents=list(docs),
        compliance_reason=None,
        compliance_decided_by=None,
        disclaimer_id=None,
    )


def _event(eid, action, *, occurred_at="2026-01-01T00:00:00+00:00"):
    return SimpleNamespace(
        id=eid,
        occurred_at=SimpleNamespace(isoformat=lambda v=occurred_at: v),
        actor_type="system",
        action=action,
        target_type="finding",
        target_id="t",
    )


def _project(pid):
    return SimpleNamespace(
        id=pid,
        sector="fintech",
        jurisdiction_scope=["CZ"],
        entity_type="s.r.o.",
        declared_intent="orientation",
    )


# ── 1a. PURE: temporal proof window logic ────────────────────────────────────
def test_brackets_as_of_window_logic():
    d = date(2025, 6, 1)
    assert evidence_api._brackets_as_of(None, date(2030, 1, 1), None) is True  # as_of None → always
    assert evidence_api._brackets_as_of(d, None, None) is True  # both bounds open
    assert (
        evidence_api._brackets_as_of(d, date(2025, 1, 1), date(2026, 1, 1)) is True
    )  # inside window
    assert evidence_api._brackets_as_of(d, date(2025, 7, 1), None) is False  # not yet in force
    assert (
        evidence_api._brackets_as_of(d, None, date(2025, 6, 1)) is False
    )  # valid_to == as_of (half-open)
    assert evidence_api._brackets_as_of(d, None, date(2025, 6, 2)) is True  # valid_to just after


def test_temporal_proof_all_brackets_and_sort():
    a, b = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    provs = [
        _prov(b, valid_from=date(2025, 1, 1), valid_to=None),  # in force
        _prov(a, valid_from=date(2030, 1, 1), valid_to=None),  # NOT yet in force at as_of
    ]
    tp = evidence_api._temporal_proof(date(2026, 1, 1), provs, {a, b})
    assert [e["provision_id"] for e in tp["provisions"]] == [a, b]  # sorted by id
    assert tp["all_brackets"] is False  # one provision out of window
    assert tp["all_cited_resolved"] is True and tp["n_resolved"] == 2  # both cited ids resolved
    tp_ok = evidence_api._temporal_proof(None, provs, {a, b})
    assert tp_ok["all_brackets"] is True  # as_of None → trivially ok


def test_temporal_proof_flags_dangling_citation():
    """A cited id that does NOT resolve to a live provision row is reported (never silently dropped) and
    makes all_cited_resolved False — the safe direction (a non-fully-grounded analysis cannot read green)."""
    a = "11111111-1111-1111-1111-111111111111"
    missing = "99999999-9999-9999-9999-999999999999"
    provs = [_prov(a, valid_from=date(2020, 1, 1), valid_to=None)]
    tp = evidence_api._temporal_proof(None, provs, {a, missing})
    assert tp["all_cited_resolved"] is False
    assert tp["unresolved_provision_ids"] == [missing]
    assert tp["n_cited"] == 2 and tp["n_resolved"] == 1


# ── 1b. PURE: refusal / grounding trail ──────────────────────────────────────
def test_refusal_trail_flags():
    p = uuid4()
    grounded = _finding(uuid4(), p)
    refusal = _finding(uuid4(), p, finding_type="refusal", answer_mode="reserved_advice")
    ungrounded = _finding(uuid4(), p, docs=[])  # 0 cited documents
    rt = evidence_api._refusal_trail([grounded, refusal])
    assert rt["any_refusal"] is True
    assert rt["all_findings_grounded"] is True  # both cite ≥1 prov + ≥1 doc
    rt2 = evidence_api._refusal_trail([grounded, ungrounded])
    assert rt2["any_refusal"] is False
    assert rt2["all_findings_grounded"] is False  # the ungrounded one breaks it


# ── 1c. PURE: full pack determinism + embedded-export cross-check ────────────
def _content_inputs():
    pid = "33333333-3333-3333-3333-333333333333"
    fid = "44444444-4444-4444-4444-444444444444"
    prov = _prov(pid, valid_from=date(2024, 1, 1), valid_to=None)
    finding = _finding(fid, pid)
    analysis_content = export_api.assemble_analysis_content(
        _project("55555555-5555-5555-5555-555555555555"),
        [finding],
        {pid: prov},
        settings=_settings(),
    )
    return dict(
        analysis_content=analysis_content,
        as_of=date(2025, 1, 1),
        provisions=[prov],
        findings=[finding],
        audit_events=[_event(1, "analysis.completed")],
        cited_provision_ids={pid},
    )


def test_build_evidence_pack_is_reproducible():
    c1, h1 = evidence_api.build_evidence_pack(**_content_inputs())
    c2, h2 = evidence_api.build_evidence_pack(**_content_inputs())
    assert c1 == c2
    assert h1 == h2 and len(h1) == 64  # byte-identical content → same hash


def test_one_field_change_moves_the_hash():
    inp = _content_inputs()
    _, h1 = evidence_api.build_evidence_pack(**inp)
    inp["as_of"] = date(2099, 1, 1)
    _, h2 = evidence_api.build_evidence_pack(**inp)
    assert h1 != h2


def test_embedded_export_hash_cross_checks_standalone():
    content, _ = evidence_api.build_evidence_pack(**_content_inputs())
    # the embedded analysis_export must hash byte-identically to a standalone audit export of the same content
    _, standalone = export_api.build_canonical_payload(content["analysis_export"])
    assert content["analysis_export_hash"] == standalone


def test_pack_carries_the_four_proof_sections():
    content, _ = evidence_api.build_evidence_pack(**_content_inputs())
    assert content["schema_version"] == evidence_api.EVIDENCE_SCHEMA_VERSION
    assert set(content) >= {
        "analysis_export",
        "analysis_export_hash",
        "temporal_proof",
        "refusal_trail",
        "audit_trail",
    }


# ── 2. GATED-OFF surface (404 when flag OFF) ─────────────────────────────────
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(evidence_api.router)
    return TestClient(app, raise_server_exceptions=True)


def test_flag_defaults_off():
    from regsuper.config import Settings  # the real settings model

    assert Settings().audit_evidence_enabled is False


def test_route_404_when_flag_off():
    with patch.object(
        evidence_api, "get_settings", lambda: _settings(audit_evidence_enabled=False)
    ):
        resp = _client().post(f"/api/evidence-pack/projects/{uuid4()}")
    assert resp.status_code == 404, resp.text


# ── 3. HAPPY-PATH route (DB mocked, flag ON) ─────────────────────────────────
class _FakeResult:
    def __init__(self, items=None, scalar=None):
        self._items = items or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    """Dispatches execute() on the selected entity (stmt.column_descriptions) so one fake serves the route's
    Finding / Provision / AnalysisRun / AuditEvent queries."""

    def __init__(self, project, findings, provisions, events, as_of):
        self.project, self.findings, self.provisions, self.events, self.as_of = (
            project,
            findings,
            provisions,
            events,
            as_of,
        )

    async def get(self, model, pk):
        return self.project if model is Project else None

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0]["entity"]
        if ent is Finding:
            return _FakeResult(items=self.findings)
        if ent is Provision:
            return _FakeResult(items=self.provisions)
        if ent is AuditEvent:
            return _FakeResult(items=self.events)
        if ent is AnalysisRun:
            return _FakeResult(scalar=self.as_of)
        return _FakeResult()


def _scope_factory(sess):
    @asynccontextmanager
    async def _scope():
        yield sess

    return _scope


def test_route_happy_path_returns_pack_and_is_reproducible():
    pid = uuid4()
    prov_id = uuid4()
    project = _project(pid)
    prov = _prov(prov_id, valid_from=date(2024, 1, 1), valid_to=None)
    finding = _finding(uuid4(), prov_id)
    sess = _FakeSession(project, [finding], [prov], [_event(1, "analysis.completed")], as_of=None)

    async def _fake_principal(_request):
        return SimpleNamespace(org_id=uuid4(), user_id=uuid4())

    app = FastAPI()
    app.include_router(evidence_api.router)
    client = TestClient(app, raise_server_exceptions=True)

    with (
        patch.object(evidence_api, "get_settings", lambda: _settings(audit_evidence_enabled=True)),
        patch.object(evidence_api, "get_principal", _fake_principal),
        patch.object(evidence_api, "session_scope", _scope_factory(sess)),
    ):
        r1 = client.post(f"/api/evidence-pack/projects/{pid}")
        r2 = client.post(f"/api/evidence-pack/projects/{pid}")

    assert r1.status_code == 200, r1.text
    body = r1.json()
    assert len(body["content_hash"]) == 64
    assert body["temporal_proof_ok"] is True  # as_of None → trivially in window
    assert body["any_refusal"] is False  # obligation, not refusal
    assert body["all_findings_grounded"] is True  # cites ≥1 provision + ≥1 document
    assert body["content"]["pack_type"] == "analysis-evidence"
    assert r2.json()["content_hash"] == body["content_hash"]  # reproducible across calls


def test_route_dangling_citation_makes_temporal_proof_not_ok():
    """End-to-end: a finding cites a provision id that does NOT resolve to a live row → temporal_proof_ok is
    False and the unresolved id is surfaced, even though the finding itself is citation-grounded (≥1 prov + doc)."""
    pid = uuid4()
    prov_id = uuid4()
    finding = _finding(uuid4(), prov_id)
    sess = _FakeSession(
        _project(pid), [finding], [], [], as_of=None
    )  # NO provisions resolve → dangling

    async def _fake_principal(_request):
        return SimpleNamespace(org_id=uuid4(), user_id=uuid4())

    app = FastAPI()
    app.include_router(evidence_api.router)
    client = TestClient(app, raise_server_exceptions=True)

    with (
        patch.object(evidence_api, "get_settings", lambda: _settings(audit_evidence_enabled=True)),
        patch.object(evidence_api, "get_principal", _fake_principal),
        patch.object(evidence_api, "session_scope", _scope_factory(sess)),
    ):
        resp = client.post(f"/api/evidence-pack/projects/{pid}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["temporal_proof_ok"] is False  # dangling → not green
    assert body["all_findings_grounded"] is True  # the finding still cites ≥1 prov + doc
    assert body["content"]["temporal_proof"]["unresolved_provision_ids"] == [str(prov_id)]


def test_route_404_when_project_missing():
    pid = uuid4()
    sess = _FakeSession(None, [], [], [], as_of=None)  # project not found

    async def _fake_principal(_request):
        return SimpleNamespace(org_id=uuid4(), user_id=uuid4())

    app = FastAPI()
    app.include_router(evidence_api.router)
    client = TestClient(app, raise_server_exceptions=True)

    with (
        patch.object(evidence_api, "get_settings", lambda: _settings(audit_evidence_enabled=True)),
        patch.object(evidence_api, "get_principal", _fake_principal),
        patch.object(evidence_api, "session_scope", _scope_factory(sess)),
    ):
        resp = client.post(f"/api/evidence-pack/projects/{pid}")
    assert resp.status_code == 404, resp.text
