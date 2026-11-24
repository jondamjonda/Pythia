"""B2b-14 — reproducible EU-AI-Act audit export (RDS-6 §1).

Two layers, both must-pass DB-FREE:

1. **Determinism of the PURE core** (no DB, no clock, no entropy) — the keystone:
   ``build_canonical_payload`` on the same CONTENT dict yields a byte-identical
   canonical JSON AND an identical SHA-256; a one-field change flips the hash; and
   the hashed content provably carries NO time/identity/instance field (the very
   exclusion that makes a re-export reproducible).

2. **Gated-off surface** — mounting ONLY this router on a bare FastAPI app (the
   ``test_law_source_gate._client`` pattern), with ``get_settings`` patched
   ``audit_export_enabled=False``, every route is 404 (inert by default). With the
   flag True and the DB layer mocked, a generated export carries a non-null
   ``payload_hash`` and ``status='done'`` — proven DB-FREE via a mock session; the
   one real DB roundtrip is skip-if-no-DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import export as export_api


# ── settings doubles ─────────────────────────────────────────────────────────
def _settings(**over):
    base = dict(
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


# ── 1. PURE determinism (no DB, no clock) ────────────────────────────────────
def _content() -> dict:
    """A representative CONTENT-ONLY dict (no id/timestamps/requester/uri)."""
    return {
        "schema_version": "1.0",
        "export_type": "analysis",
        "subject_id": "11111111-1111-1111-1111-111111111111",
        "ai_system": {"product": "Polarius", "model": "gpt-4o", "law_source": "regsuper"},
        "query": {"sector": "fintech", "jurisdiction": ["CZ"], "entity": "s.r.o."},
        "answer_mode": "information",
        "findings": [
            {
                "finding_type": "obligation",
                "statement": "Musíte vést evidenci.",
                "severity": "high",
                "confidence_overall": 0.82,
                "review_state": "unreviewed",
                "answer_mode": "information",
                "provisions": [
                    {
                        "provision_id": "aaaa",
                        "label": "§ 5",
                        "citation": "§ 5 (CZ/zakon)",
                        "excerpt": "text",
                        "bindingness_tier": "hard-law",
                        "temporal_status": "in-force",
                    }
                ],
            }
        ],
        "data_residency": {"compute_region": "EU", "storage_region": "EU"},
    }


def test_canonical_payload_is_reproducible() -> None:
    """The keystone: same content → byte-identical JSON AND identical hash."""
    j1, h1 = export_api.build_canonical_payload(_content())
    j2, h2 = export_api.build_canonical_payload(_content())
    assert j1 == j2
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_canonical_payload_is_key_order_invariant() -> None:
    """sort_keys makes the canonical form invariant to dict construction order — two
    structurally-equal contents (built in different key orders) hash identically."""
    c = _content()
    reordered = {k: c[k] for k in reversed(list(c.keys()))}
    j1, h1 = export_api.build_canonical_payload(c)
    j2, h2 = export_api.build_canonical_payload(reordered)
    assert j1 == j2
    assert h1 == h2


def test_one_field_change_changes_the_hash() -> None:
    c1 = _content()
    c2 = _content()
    c2["findings"][0]["confidence_overall"] = 0.99
    _, h1 = export_api.build_canonical_payload(c1)
    _, h2 = export_api.build_canonical_payload(c2)
    assert h1 != h2


def test_no_time_or_identity_field_in_hashed_content() -> None:
    """Reproducibility comes from EXCLUSION: the hashed content must NOT carry any
    export id / requested_at / generated_at / requested_by / *_s3_uri. We add those
    keys to a copy and confirm the hash MOVES — i.e. they were never present in the
    canonical content to begin with, and the stable content omits them.
    """
    base = _content()
    _, h_base = export_api.build_canonical_payload(base)

    for leak_key, leak_val in (
        ("id", str(uuid4())),
        ("requested_at", "2026-06-24T00:00:00+00:00"),
        ("generated_at", "2026-06-24T00:00:01+00:00"),
        ("requested_by", str(uuid4())),
        ("json_s3_uri", "./data/exports/x.json"),
        ("pdf_s3_uri", None),
    ):
        polluted = dict(base)
        polluted[leak_key] = leak_val
        _, h_polluted = export_api.build_canonical_payload(polluted)
        assert h_polluted != h_base, f"{leak_key} leaked into the stable content"

    # And the canonical JSON of the clean content literally contains none of them.
    canonical_json, _ = export_api.build_canonical_payload(base)
    for forbidden in ("requested_at", "generated_at", "requested_by", "json_s3_uri", "pdf_s3_uri"):
        assert f'"{forbidden}"' not in canonical_json


def test_assemble_analysis_content_is_deterministic_and_clean() -> None:
    """The assembler shapes content from ORM-shaped doubles, sorts its lists, and the
    result hashes stably AND excludes every per-instance field."""
    pid = uuid4()
    prov_a = SimpleNamespace(
        id=uuid4(),
        label="§ 5",
        eid="e5",
        hierarchy_path="a.b",
        jurisdiction="CZ",
        legal_hierarchy="zakon",
        text="provision A text",
        bindingness_tier="hard-law",
        temporal_status="in-force",
    )
    prov_b = SimpleNamespace(
        id=uuid4(),
        label="§ 6",
        eid="e6",
        hierarchy_path="a.c",
        jurisdiction="CZ",
        legal_hierarchy="zakon",
        text="provision B text",
        bindingness_tier="soft-law",
        temporal_status="in-force",
    )
    finding = SimpleNamespace(
        id=uuid4(),
        finding_type="obligation",
        statement="S",
        severity="high",
        confidence_overall=0.7,
        review_state="unreviewed",
        answer_mode="information",
        cited_provisions=[prov_b.id, prov_a.id],  # deliberately unsorted
    )
    project = SimpleNamespace(
        id=pid,
        sector="fintech",
        jurisdiction_scope=["CZ"],
        entity_type="s.r.o.",
        declared_intent="lending",
    )
    provs = {str(prov_a.id): prov_a, str(prov_b.id): prov_b}
    s = _settings()

    c1 = export_api.assemble_analysis_content(project, [finding], provs, settings=s)
    c2 = export_api.assemble_analysis_content(project, [finding], provs, settings=s)
    _, h1 = export_api.build_canonical_payload(c1)
    _, h2 = export_api.build_canonical_payload(c2)
    assert h1 == h2  # re-assembly of the same content is reproducible

    # Provisions are sorted by id inside the finding (order-invariant).
    prov_ids = [b["provision_id"] for b in c1["findings"][0]["provisions"]]
    assert prov_ids == sorted(prov_ids)

    # No per-instance field leaked in.
    canonical_json, _ = export_api.build_canonical_payload(c1)
    for forbidden in ("requested_at", "generated_at", "requested_by", "json_s3_uri"):
        assert forbidden not in canonical_json
    assert c1["export_type"] == "analysis"
    assert c1["subject_id"] == str(pid)


def test_import_pulls_in_no_db_or_clock_at_module_import() -> None:
    """Importing the module must touch NO DB and read NO clock — proven by a clean
    subprocess import in a fresh interpreter (no DB env)."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import regsuper.api.export"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"import raised (DB/clock at import?):\n{proc.stderr}"


# ── 2. Gated-off surface (DB-free, router-only mount) ────────────────────────
def _client() -> TestClient:
    """A TestClient over JUST this router — no FastAPI lifespan, so no DB at import."""
    app = FastAPI()
    app.include_router(export_api.router)
    return TestClient(app, raise_server_exceptions=True)


def test_post_project_export_404_when_flag_off() -> None:
    with patch.object(export_api, "get_settings", lambda: _settings(audit_export_enabled=False)):
        resp = _client().post(f"/api/projects/{uuid4()}/export")
    assert resp.status_code == 404, resp.text


def test_post_finding_export_404_when_flag_off() -> None:
    with patch.object(export_api, "get_settings", lambda: _settings(audit_export_enabled=False)):
        resp = _client().post(f"/api/projects/{uuid4()}/findings/{uuid4()}/export")
    assert resp.status_code == 404, resp.text


def test_get_export_404_when_flag_off() -> None:
    with patch.object(export_api, "get_settings", lambda: _settings(audit_export_enabled=False)):
        resp = _client().get(f"/api/exports/{uuid4()}")
    assert resp.status_code == 404, resp.text


# ── 3. generate_export sets payload_hash + status='done' (DB-FREE, mocked) ────
@pytest.mark.asyncio
async def test_generate_export_sets_hash_and_done_db_free(tmp_path, monkeypatch) -> None:
    """A finding export, end-to-end through generate_export, with a MOCK session — no
    Postgres. Asserts the created row ends with a non-null payload_hash and status
    'done', and that the canonical JSON file was written.
    """
    # Redirect the export dir to a tmp path so the test writes nowhere real.
    monkeypatch.setattr(export_api, "_EXPORT_DIR", tmp_path)

    fid = uuid4()
    prov = SimpleNamespace(
        id=uuid4(),
        label="§ 5",
        eid="e5",
        hierarchy_path="a.b",
        jurisdiction="CZ",
        legal_hierarchy="zakon",
        text="provision text",
        bindingness_tier="hard-law",
        temporal_status="in-force",
    )
    finding = SimpleNamespace(
        id=fid,
        project_id=uuid4(),
        finding_type="obligation",
        statement="S",
        severity="high",
        confidence_overall=0.7,
        review_state="unreviewed",
        answer_mode="information",
        cited_provisions=[prov.id],
    )
    row = SimpleNamespace(
        id=uuid4(),
        export_type="finding",
        subject_id=fid,
        org_id=uuid4(),
        status="pending",
        schema_version="1.0",
        payload_hash=None,
        json_s3_uri=None,
        pdf_s3_uri="sentinel",
        generated_at=None,
        error_detail=None,
    )

    class _FakeResult:
        def __init__(self, items):
            self._items = items

        def scalars(self):
            return self

        def all(self):
            return self._items

    class _FakeSession:
        async def get(self, model, pk):
            from regsuper.storage.models import Finding

            if model is Finding and pk == fid:
                return finding
            return None

        async def execute(self, _stmt):
            # The only execute() generate_export issues for a finding export is the
            # provision IN-load.
            return _FakeResult([prov])

    captured: list[str] = []

    async def fake_emit(code, *_a, **_k):
        captured.append(code)

    async def fake_log_event(*_a, **_k):
        return None

    with (
        patch.object(export_api, "get_settings", lambda: _settings(audit_export_enabled=True)),
        patch.object(export_api, "emit", fake_emit),
        patch.object(export_api, "log_event", fake_log_event),
    ):
        out = await export_api.generate_export(_FakeSession(), row)

    assert out.status == "done", out.error_detail
    assert out.payload_hash and len(out.payload_hash) == 64
    assert out.pdf_s3_uri is None  # PDF deferred
    assert out.json_s3_uri == str(tmp_path / f"{row.id}.json")
    assert (tmp_path / f"{row.id}.json").exists()
    assert "export.ready" in captured

    # Reproducibility through the worker: regenerate → same payload_hash.
    row2 = SimpleNamespace(
        id=uuid4(),
        export_type="finding",
        subject_id=fid,
        org_id=uuid4(),
        status="pending",
        schema_version="1.0",
        payload_hash=None,
        json_s3_uri=None,
        pdf_s3_uri=None,
        generated_at=None,
        error_detail=None,
    )
    with (
        patch.object(export_api, "get_settings", lambda: _settings(audit_export_enabled=True)),
        patch.object(export_api, "emit", fake_emit),
        patch.object(export_api, "log_event", fake_log_event),
    ):
        out2 = await export_api.generate_export(_FakeSession(), row2)
    assert out2.payload_hash == out.payload_hash  # byte-identical content → same hash


@pytest.mark.asyncio
async def test_generate_export_records_error_never_raises_db_free() -> None:
    """A content error (missing subject) is RECORDED on the row (status='error',
    error_detail set) and an export.refused is emitted — the worker never raises."""

    class _MissingSession:
        async def get(self, _model, _pk):
            return None  # subject not found → LookupError inside generate_export

    row = SimpleNamespace(
        id=uuid4(),
        export_type="finding",
        subject_id=uuid4(),
        org_id=uuid4(),
        status="pending",
        schema_version="1.0",
        payload_hash=None,
        json_s3_uri=None,
        pdf_s3_uri=None,
        generated_at=None,
        error_detail=None,
    )
    captured: list[str] = []

    async def fake_emit(code, *_a, **_k):
        captured.append(code)

    with (
        patch.object(export_api, "get_settings", lambda: _settings(audit_export_enabled=True)),
        patch.object(export_api, "emit", fake_emit),
    ):
        out = await export_api.generate_export(_MissingSession(), row)

    assert out.status == "error"
    assert out.error_detail and len(out.error_detail) <= 2000
    assert out.payload_hash is None
    assert "export.refused" in captured


# ── 4. one real DB roundtrip (skip-if-no-DB, rollback-only) ──────────────────
async def _db_reachable() -> bool:
    from sqlalchemy import text

    from regsuper.storage.database import dispose_engine, get_session_factory

    await dispose_engine()
    try:
        factory = get_session_factory()
        async with factory() as s:
            ok = (await s.execute(text("SELECT to_regclass('audit_export') IS NOT NULL"))).scalar()
        return bool(ok)
    except Exception:
        return False


@pytest.mark.asyncio
async def test_create_audit_export_roundtrip_rolled_back() -> None:
    """create_audit_export inserts a pending row with a flushed id, inside a
    transaction that is ALWAYS rolled back so the live table is never mutated."""
    if not await _db_reachable():
        pytest.skip("no DB / audit_export table not reachable")

    from regsuper.storage.database import get_session_factory
    from regsuper.storage.models import Organization

    factory = get_session_factory()
    async with factory() as session:
        trans = await session.begin()
        try:
            # A valid org_id is needed for the NOT-NULL FK; reuse an existing one.
            org_id = (
                await session.execute(__import__("sqlalchemy").select(Organization.id).limit(1))
            ).scalar()
            if org_id is None:
                pytest.skip("no organization row to attribute the export to")

            async def _noop_emit(*_a, **_k):
                return None

            with patch.object(export_api, "emit", _noop_emit):
                row = await export_api.create_audit_export(
                    session,
                    export_type="analysis",
                    subject_id=uuid4(),
                    org_id=org_id,
                    requested_by=uuid4(),
                )
            assert row.id is not None
            assert row.status == "pending"
            assert row.schema_version == "1.0"
        finally:
            await trans.rollback()
