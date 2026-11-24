"""T-ANS-M7 — source-viewer cross-boundary resolution (C4-18).

DB-FREE coverage of ``GET /api/provisions/{id}`` over the router mounted on a bare
FastAPI app (the ``test_digest._client`` pattern — no lifespan, no real DB). The
module-level data accessors (``get_graph_engine`` / ``graph_session_scope`` /
``session_scope`` / ``expand``) are patched so every branch is exercised offline.

The M7 property under test: a ``?corpus=pythia`` chip id that is ABSENT in the
pythia corpus (an un-cutover domain) is resolved across the seam via
``provision_id_map`` to the regsuper provision — it never dead-ends at a 404 — and
an honest 404 (NOT a 500) is returned only when NEITHER corpus nor the crosswalk
resolves. The DEFAULT (no ?corpus) app-DB path is byte-identical: same shape, same
404, and NO provenance header.

Acceptance rows:
  1. cutover-domain id (present in pythia corpus) → served from pythia (header=pythia).
  2. un-cutover id (absent in pythia, present in crosswalk) → regsuper via id-map
     (header=regsuper, no 404 across the seam).
  3. neither (absent in pythia AND no crosswalk row) → honest 404, never 500.
  4. default path (no ?corpus) byte-identical: served from app DB, NO provenance
     header; missing id → 404.
  5. robustness: corpus DB unconfigured + crosswalk present → still resolves via the
     map (404-not-500, never a hard "not configured" dead-end when the seam can cross).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from regsuper.api import provisions as prov_api

# A fixed pythia/nexus chip id and the regsuper provision it crosswalks to.
PYTHIA_ID = UUID("11111111-1111-1111-1111-111111111111")
REGSUPER_ID = UUID("22222222-2222-2222-2222-222222222222")
SRC_DOC_ID = UUID("33333333-3333-3333-3333-333333333333")


def _client() -> TestClient:
    """A TestClient over JUST the provisions router — no lifespan, no DB at import."""
    app = FastAPI()
    app.include_router(prov_api.router)
    return TestClient(app, raise_server_exceptions=True)


# ── fake DB rows (tuples matching the SELECT column order) ────────────────────
def _corpus_row(pid: UUID) -> tuple:
    # _CORPUS_ROW_SQL: id,label,text,text_format,hierarchy_path,legal_hierarchy,
    # jurisdiction,language,source_document_id,parent_id,external_id,title,publisher
    return (
        pid,
        "§ 31",
        "pythia corpus text",
        "plaintext",
        "act.chap6.art31",
        "act",
        "EU",
        "cs",
        SRC_DOC_ID,
        None,
        "2011/61/EU",
        "AIFMD",
        "EUR-Lex",
    )


def _app_row(pid: UUID) -> tuple:
    # _APP_ROW_SQL: ...,external_id,title,publisher,bindingness_tier,temporal_status
    return (
        pid,
        "§ 5",
        "regsuper app text",
        "plaintext",
        "act.sec5",
        "act",
        "CZ",
        "cs",
        SRC_DOC_ID,
        None,
        "240/2013",
        "ZISIF",
        "Sbírka",
        None,
        None,
    )


def _id_map_row(regsuper_id: UUID) -> tuple:
    return (regsuper_id,)


class _FakeResult:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeSession:
    """Routes each execute() to a canned result keyed by which SQL is run."""

    def __init__(self, *, row=None, ancestors=None, xrefs=None, id_map=None):
        self._row = row
        self._ancestors = ancestors or []
        self._xrefs = xrefs or []
        self._id_map = id_map

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "provision_id_map" in sql:
            return _FakeResult([self._id_map] if self._id_map is not None else [])
        if "@>" in sql:  # ancestors
            return _FakeResult(self._ancestors)
        if "cross_reference" in sql:  # xrefs
            return _FakeResult(self._xrefs)
        return _FakeResult([self._row] if self._row is not None else [])


def _scope(session):
    @asynccontextmanager
    async def _cm():
        yield session

    return _cm


def _expand_none(_pid):
    async def _inner():
        return None

    return _inner()


# ── 1. cutover-domain id present in the pythia corpus → served from pythia ─────
def test_corpus_id_present_served_from_pythia():
    corpus_session = _FakeSession(row=_corpus_row(PYTHIA_ID))
    with (
        patch.object(prov_api, "get_graph_engine", lambda: object()),
        patch.object(prov_api, "graph_session_scope", _scope(corpus_session)),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(PYTHIA_ID)
    assert body["text"] == "pythia corpus text"
    assert body["source_external_id"] == "2011/61/EU"
    # corpus DB predates the trust columns → NULL, never fabricated.
    assert body["bindingness_tier"] is None
    assert body["temporal_status"] is None
    assert resp.headers.get("X-Provision-Corpus") == "pythia"


def test_graph_alias_also_reads_pythia():
    corpus_session = _FakeSession(row=_corpus_row(PYTHIA_ID))
    with (
        patch.object(prov_api, "get_graph_engine", lambda: object()),
        patch.object(prov_api, "graph_session_scope", _scope(corpus_session)),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=graph")
    assert resp.status_code == 200
    assert resp.headers.get("X-Provision-Corpus") == "pythia"


# ── 2. un-cutover id absent in pythia, present in crosswalk → regsuper via map ─
def test_uncutover_id_resolves_to_regsuper_via_id_map():
    corpus_session = _FakeSession(row=None)  # absent in pythia corpus
    app_session = _FakeSession(row=_app_row(REGSUPER_ID), id_map=_id_map_row(REGSUPER_ID))
    with (
        patch.object(prov_api, "get_graph_engine", lambda: object()),
        patch.object(prov_api, "graph_session_scope", _scope(corpus_session)),
        patch.object(prov_api, "session_scope", _scope(app_session)),
        patch.object(prov_api, "expand", _expand_none),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 200, resp.text  # NO 404 across the seam
    body = resp.json()
    assert body["id"] == str(REGSUPER_ID)
    assert body["text"] == "regsuper app text"
    assert body["source_external_id"] == "240/2013"
    assert resp.headers.get("X-Provision-Corpus") == "regsuper"


def test_uncutover_fallback_serves_real_regsuper_view_with_ancestors():
    # The fallback reuses the app-DB read incl. expand() → ancestors flow through.
    corpus_session = _FakeSession(row=None)
    app_session = _FakeSession(row=_app_row(REGSUPER_ID), id_map=_id_map_row(REGSUPER_ID))
    anc = SimpleNamespace(id=SRC_DOC_ID, label="Act", hierarchy_path="act")

    def _expand_with_anc(_pid):
        async def _inner():
            return SimpleNamespace(ancestors=[anc], cross_references=[])

        return _inner()

    with (
        patch.object(prov_api, "get_graph_engine", lambda: object()),
        patch.object(prov_api, "graph_session_scope", _scope(corpus_session)),
        patch.object(prov_api, "session_scope", _scope(app_session)),
        patch.object(prov_api, "expand", _expand_with_anc),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 200
    body = resp.json()
    assert [a["label"] for a in body["ancestors"]] == ["Act"]


# ── 3. neither resolves → honest 404 (never 500) ──────────────────────────────
def test_neither_pythia_nor_map_resolves_is_honest_404():
    corpus_session = _FakeSession(row=None)  # absent in pythia
    app_session = _FakeSession(row=None, id_map=None)  # no crosswalk row
    with (
        patch.object(prov_api, "get_graph_engine", lambda: object()),
        patch.object(prov_api, "graph_session_scope", _scope(corpus_session)),
        patch.object(prov_api, "session_scope", _scope(app_session)),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()
    # honest reason names BOTH the missing corpus row and the missing crosswalk.
    assert "crosswalk" in resp.json()["detail"].lower()


def test_crosswalk_present_but_mapped_provision_missing_is_404_not_500():
    # The map row exists but the regsuper provision it points at is gone → 404, not 500.
    corpus_session = _FakeSession(row=None)
    app_session = _FakeSession(row=None, id_map=_id_map_row(REGSUPER_ID))
    with (
        patch.object(prov_api, "get_graph_engine", lambda: object()),
        patch.object(prov_api, "graph_session_scope", _scope(corpus_session)),
        patch.object(prov_api, "session_scope", _scope(app_session)),
        patch.object(prov_api, "expand", _expand_none),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 404


# ── 5. robustness: corpus DB unconfigured + crosswalk present → resolves ───────
def test_corpus_unconfigured_but_crosswalk_present_resolves_via_map():
    app_session = _FakeSession(row=_app_row(REGSUPER_ID), id_map=_id_map_row(REGSUPER_ID))
    with (
        patch.object(prov_api, "get_graph_engine", lambda: None),
        patch.object(prov_api, "session_scope", _scope(app_session)),
        patch.object(prov_api, "expand", _expand_none),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(REGSUPER_ID)
    assert resp.headers.get("X-Provision-Corpus") == "regsuper"


def test_corpus_unconfigured_and_no_crosswalk_is_404_not_500():
    app_session = _FakeSession(row=None, id_map=None)
    with (
        patch.object(prov_api, "get_graph_engine", lambda: None),
        patch.object(prov_api, "session_scope", _scope(app_session)),
    ):
        resp = _client().get(f"/api/provisions/{PYTHIA_ID}?corpus=pythia")
    assert resp.status_code == 404


# ── 4. DEFAULT path (no ?corpus) byte-identical ───────────────────────────────
def test_default_path_served_from_app_db_no_provenance_header():
    app_session = _FakeSession(row=_app_row(REGSUPER_ID))
    with (
        patch.object(prov_api, "session_scope", _scope(app_session)),
        patch.object(prov_api, "expand", _expand_none),
    ):
        resp = _client().get(f"/api/provisions/{REGSUPER_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(REGSUPER_ID)
    assert body["text"] == "regsuper app text"
    assert body["bindingness_tier"] is None
    assert body["temporal_status"] is None
    # the provenance header is a ?corpus-only signal → ABSENT on the default path.
    assert "X-Provision-Corpus" not in resp.headers


def test_default_path_does_not_consult_the_crosswalk():
    # A missing id on the default path 404s WITHOUT ever touching provision_id_map
    # (no cross-boundary fallback on the bare app-DB read).
    seen_sql: list[str] = []

    class _TrackingSession(_FakeSession):
        async def execute(self, statement, params=None):
            seen_sql.append(str(statement))
            return await super().execute(statement, params)

    app_session = _TrackingSession(row=None, id_map=_id_map_row(REGSUPER_ID))
    with patch.object(prov_api, "session_scope", _scope(app_session)):
        resp = _client().get(f"/api/provisions/{REGSUPER_ID}")
    assert resp.status_code == 404
    assert not any("provision_id_map" in s for s in seen_sql)


def test_default_path_missing_id_is_404():
    app_session = _FakeSession(row=None)
    with patch.object(prov_api, "session_scope", _scope(app_session)):
        resp = _client().get(f"/api/provisions/{uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["detail"].startswith("provision ")


# ── id-map lookup pins nexus_corpus='pythia' (the UNIQUE-pair contract) ────────
def test_id_map_lookup_pins_pythia_corpus():
    # provision_id_map is unique on (nexus_id, nexus_corpus); the fallback MUST
    # pin nexus_corpus='pythia' so the right regsuper row is resolved.
    assert "nexus_corpus = 'pythia'" in prov_api._ID_MAP_SQL
    assert "nexus_id = :nid" in prov_api._ID_MAP_SQL
