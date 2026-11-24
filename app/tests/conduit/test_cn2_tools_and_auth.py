"""T-CN2 — the three pure-READ Conduit tools + the API-key auth channel.

The highest-scrutiny surface in the product, exercised with the Conduit service OFF:
every test mocks the engine read boundaries (`retrieval.retrieve`, `expand`,
`pythia.client.act`) and the DB (`session_scope`) so the suite needs no running
listener, no broker, no network, and no live corpus. The cardinal invariant under
test: every tool result returns a NON-EMPTY citation through `envelope.finalize`, or
it refuses — never an uncited claim.

Coverage (manifest T-CN2 acceptance, doc-05 §3 Tools 1-3 + §4.2):
  * search_czech_law: a topic → ranked provisions each cited; off-corpus → refused
    no_coverage; as_of post-filter excludes a future-dated provision.
  * get_provision: known id → verbatim text + breadcrumb + provision-as-citation;
    unknown id → out_of_scope.
  * whats_in_force: collapses status honestly with grounding='fuzzy'.
  * API-key channel: valid key → ConduitPrincipal{org_id, scopes}; invalid/revoked →
    401 + PRM WWW-Authenticate pointer; the secret is never stored or logged.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from regsuper.conduit import auth as conduit_auth
from regsuper.conduit import tools
from regsuper.conduit.auth import ConduitPrincipal
from regsuper.conduit.envelope import RefusalReason
from regsuper.conduit.schemas import (
    GetProvisionInput,
    SearchCzechLawInput,
    WhatsInForceInput,
)


# ── fake engine result shapes ────────────────────────────────────────────────
@dataclass
class _FakeHit:
    provision_id: UUID
    rerank_score: float | None = 0.82


@dataclass
class _FakeResult:
    candidates: list = field(default_factory=list)
    payloads: dict = field(default_factory=dict)
    refused: bool = False
    flagged: bool = False
    refusal_source: str | None = None
    top_rerank_score: float | None = 0.82


class _FakeRows:
    """Mimics the SQLAlchemy Result `.all()` / `.first()` surface."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def _fake_session_scope(execute_handler):
    """Build an `@asynccontextmanager` `session_scope` whose `.execute(sql, params)`
    delegates to `execute_handler(sql, params) -> list[rows]`."""

    @asynccontextmanager
    async def _scope():
        class _S:
            async def execute(self, sql, params=None):
                return _FakeRows(execute_handler(str(sql), params or {}))

        yield _S()

    return _scope


# =============================================================================
# A. search_czech_law
# =============================================================================
async def test_search_topic_returns_ranked_provisions_each_cited(monkeypatch):
    """A topic returns ranked provisions, and EVERY result carries a citation
    (the envelope is not refused)."""
    pid1, pid2 = uuid4(), uuid4()
    result = _FakeResult(
        candidates=[_FakeHit(pid1, 0.9), _FakeHit(pid2, 0.7)],
        payloads={
            pid1: {
                "label": "§ 5",
                "external_id": "110/2019",
                "title": "Zákon o ZOÚ",
                "legal_hierarchy": "cz-act",
                "jurisdiction": "CZ",
                "text": "Some legal text.",
            },
            pid2: {
                "label": "Art 6",
                "external_id": "GDPR",
                "title": "GDPR",
                "legal_hierarchy": "eu-regulation",
                "jurisdiction": "EU",
                "text": "Lawfulness.",
            },
        },
        top_rerank_score=0.9,
    )

    async def _fake_retrieve(query, **kwargs):
        return result

    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)
    # No temporal rows for these ids (NULL valid_from/to → treated in force).
    monkeypatch.setattr(
        "regsuper.storage.database.session_scope",
        _fake_session_scope(lambda sql, p: []),
    )

    env = await tools.search_czech_law(SearchCzechLawInput(query="osobní údaje"))

    assert env.refused is False
    assert env.data["n_results"] == 2
    rows = env.data["results"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert all(r["provision_id"] for r in rows)
    # Every surviving result has a matching citation through finalize.
    assert len(env.citations) == 2
    assert {c.provision_id for c in env.citations} == {str(pid1), str(pid2)}
    assert env.trust.grounding == "verified"
    assert "not legal advice" in env.disclaimer


async def test_search_off_corpus_refuses_no_coverage(monkeypatch):
    """An off-corpus topic with zero candidates → refused no_coverage (never an empty
    uncited result list shown as an answer)."""

    async def _fake_retrieve(query, **kwargs):
        return _FakeResult(candidates=[], payloads={}, top_rerank_score=None)

    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)
    monkeypatch.setattr(
        "regsuper.storage.database.session_scope",
        _fake_session_scope(lambda sql, p: []),
    )

    env = await tools.search_czech_law(SearchCzechLawInput(query="how to bake a cake"))
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.NO_COVERAGE
    assert env.data is None


async def test_search_as_of_postfilter_excludes_future_dated_provision(monkeypatch):
    """With a past `as_of`, a provision whose `valid_from` is AFTER as_of is excluded
    by the point-in-time post-filter (doc-05 §3 Tool 1)."""
    in_force_pid, future_pid = uuid4(), uuid4()
    result = _FakeResult(
        candidates=[_FakeHit(in_force_pid, 0.9), _FakeHit(future_pid, 0.85)],
        payloads={
            in_force_pid: {
                "label": "§ 1",
                "external_id": "A",
                "text": "old text",
                "jurisdiction": "CZ",
            },
            future_pid: {
                "label": "§ 99",
                "external_id": "B",
                "text": "new text",
                "jurisdiction": "CZ",
            },
        },
        top_rerank_score=0.9,
    )

    async def _fake_retrieve(query, **kwargs):
        return result

    as_of = date(2020, 1, 1)
    # in_force valid from 2015 (brackets as_of); future valid from 2026 (after as_of).
    temporal = {
        in_force_pid: (in_force_pid, date(2015, 1, 1), None),
        future_pid: (future_pid, date(2026, 1, 1), None),
    }

    def _exec(sql, params):
        # the temporal lookup SELECTs valid_from/valid_to for the candidate ids
        return [(pid, vf, vt) for pid, vf, vt in temporal.values()]

    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    env = await tools.search_czech_law(SearchCzechLawInput(query="topic", as_of=as_of))
    assert env.refused is False
    ids = {r["provision_id"] for r in env.data["results"]}
    assert str(in_force_pid) in ids
    assert str(future_pid) not in ids  # the future-dated provision was excluded
    assert env.data["n_results"] == 1


# =============================================================================
# B. get_provision
# =============================================================================
async def test_get_provision_known_id_returns_text_breadcrumb_and_citation(monkeypatch):
    """A known id returns verbatim text + a breadcrumb + the provision AS its own
    citation (the single returned provision IS the citation)."""
    pid = uuid4()
    parent = uuid4()
    sdid = uuid4()

    hit = SimpleNamespace(
        id=pid,
        label="§ 15",
        text="The controller shall keep records.",
        legal_hierarchy="cz-act",
        jurisdiction="CZ",
        source_document_id=sdid,
    )
    ancestor = SimpleNamespace(id=parent, label="Část II", hierarchy_path="a.b")
    expanded = SimpleNamespace(hit=hit, ancestors=[ancestor], cross_references=[], definitions=[])

    async def _fake_expand(provision_id):
        assert provision_id == pid
        return expanded

    def _exec(sql, params):
        low = sql.lower()
        if "valid_from" in low and "valid_to" in low and "source_document" not in low:
            return [(pid, date(2013, 1, 1), None)]  # temporal for the provision
        if (
            "source_document" in low
            and "title" in low
            and "external_id" in low
            and "id = :sid" in low
        ):
            return [("240/2013", "Zákon o investičních společnostech")]  # act meta
        return []

    # `act` is a bare UUID → _resolve_provision_id returns it directly (no DB hit).
    # NOTE: the package __init__ re-exports a FUNCTION named `expand`, which shadows the
    # `expand` submodule on attribute access — so reach the real module via sys.modules
    # and patch its function there (the tool does `from regsuper.retrieval.expand import
    # expand` at call time, picking up the patched module attribute).
    import importlib

    expand_mod = importlib.import_module("regsuper.retrieval.expand")
    monkeypatch.setattr(expand_mod, "expand", _fake_expand)
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    env = await tools.get_provision(GetProvisionInput(act=str(pid)))
    assert env.refused is False
    assert env.data["text"] == "The controller shall keep records."
    assert env.data["provision_id"] == str(pid)
    assert env.data["external_id"] == "240/2013"
    # breadcrumb rendered top-down (ancestor present)
    assert env.data["breadcrumb"] == [{"id": str(parent), "label": "Část II"}]
    # the provision IS the citation
    assert len(env.citations) == 1
    assert env.citations[0].provision_id == str(pid)
    assert env.citations[0].external_id == "240/2013"
    assert env.trust.grounding == "verified"


async def test_get_provision_unknown_id_refuses_out_of_scope(monkeypatch):
    """An id that resolves to nothing → out_of_scope (no fabricated content)."""
    # A non-UUID locator that resolves to no row.
    monkeypatch.setattr(
        "regsuper.storage.database.session_scope",
        _fake_session_scope(lambda sql, p: []),
    )

    env = await tools.get_provision(GetProvisionInput(act="999/9999"))
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.OUT_OF_SCOPE
    assert env.data is None


# =============================================================================
# C. whats_in_force
# =============================================================================
async def test_whats_in_force_collapses_status_with_fuzzy_grounding(monkeypatch):
    """whats_in_force collapses each provision's status honestly from
    valid_from/valid_to AND marks grounding='fuzzy' (no faked amendment history)."""
    p_in_force, p_repealed, p_future = uuid4(), uuid4(), uuid4()
    as_of = date(2024, 6, 1)
    rows = [
        (p_in_force, "§ 1", date(2013, 1, 1), None, "240/2013", "Investiční zákon", "CZ"),
        (
            p_repealed,
            "§ 2",
            date(2013, 1, 1),
            date(2020, 1, 1),
            "240/2013",
            "Investiční zákon",
            "CZ",
        ),
        (p_future, "§ 3", date(2026, 1, 1), None, "240/2013", "Investiční zákon", "CZ"),
    ]

    def _exec(sql, params):
        if "source_document" in sql.lower() and "order by" in sql.lower():
            return rows
        return []

    # law_source='regsuper' (default) → goes straight to the app-DB read path.
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(law_source="regsuper", pythia_enabled=False, pythia_url=None),
    )
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    env = await tools.whats_in_force(WhatsInForceInput(act="240/2013", as_of=as_of))
    assert env.refused is False
    assert env.trust.grounding == "fuzzy"
    assert env.data["in_force"] is True  # at least one in-force provision
    by_id = {p["provision_id"]: p["status"] for p in env.data["provisions"]}
    assert by_id[str(p_in_force)] == "in_force"
    assert by_id[str(p_repealed)] == "repealed"
    assert by_id[str(p_future)] == "not_yet_in_force"
    # every provision is a citation
    assert len(env.citations) == 3


async def test_whats_in_force_unknown_act_refuses_out_of_scope(monkeypatch):
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(law_source="regsuper", pythia_enabled=False, pythia_url=None),
    )
    monkeypatch.setattr(
        "regsuper.storage.database.session_scope",
        _fake_session_scope(lambda sql, p: []),
    )

    env = await tools.whats_in_force(WhatsInForceInput(act="000/0000"))
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.OUT_OF_SCOPE


# =============================================================================
# D. API-key auth channel
# =============================================================================
async def test_validate_api_key_valid_returns_principal(monkeypatch):
    """A valid key → ConduitPrincipal{org_id, scopes} resolved from the active
    conduit_credential row; auth_kind='api_key'."""
    org = uuid4()
    cid = "conduit:abc123def456"

    def _exec(sql, params):
        # the credential lookup by hash
        return [(org, cid, ["corpus.read", "obligations.read"], "active")]

    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    principal = await conduit_auth.validate_api_key("a-real-secret")
    assert isinstance(principal, ConduitPrincipal)
    assert principal.org_id == str(org)
    assert principal.credential_id == cid
    assert principal.scopes == frozenset({"corpus.read", "obligations.read"})
    assert principal.auth_kind == "api_key"


async def test_validate_api_key_unknown_raises_401_with_prm(monkeypatch):
    """An unknown key → 401 carrying the PRM WWW-Authenticate pointer."""
    from fastapi import HTTPException

    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(conduit_resource_uri="https://api.polarius.cz/conduit"),
    )
    monkeypatch.setattr(
        "regsuper.storage.database.session_scope",
        _fake_session_scope(lambda sql, p: []),  # no matching row
    )

    with pytest.raises(HTTPException) as ei:
        await conduit_auth.validate_api_key("bogus")
    assert ei.value.status_code == 401
    wwwa = ei.value.headers.get("WWW-Authenticate", "")
    assert wwwa.startswith("Bearer")
    assert "oauth-protected-resource" in wwwa


async def test_validate_api_key_revoked_raises_401(monkeypatch):
    """A revoked credential (status != 'active') → 401 (treated identically to
    unknown so the channel does not leak which)."""
    from fastapi import HTTPException

    org = uuid4()

    def _exec(sql, params):
        return [(org, "conduit:deadbeef0000", ["corpus.read"], "revoked")]

    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(conduit_resource_uri="https://api.polarius.cz/conduit"),
    )
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    with pytest.raises(HTTPException) as ei:
        await conduit_auth.validate_api_key("revoked-secret")
    assert ei.value.status_code == 401


async def test_validate_api_key_empty_raises_401(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(conduit_resource_uri=None),
    )
    with pytest.raises(HTTPException) as ei:
        await conduit_auth.validate_api_key("   ")
    assert ei.value.status_code == 401
    # With no resource uri configured we still emit a bare Bearer challenge.
    assert ei.value.headers.get("WWW-Authenticate") == "Bearer"


async def test_validate_api_key_never_logs_the_secret(monkeypatch, caplog):
    """The presented secret must NEVER appear in logs — only the hash + org_id leave
    the function."""
    org = uuid4()
    secret = "TOP-SECRET-LEAKY-VALUE-12345"
    cid = "conduit:abc123def456"

    def _exec(sql, params):
        # the param bound for the lookup MUST be the hash, never the raw secret
        assert secret not in str(params)
        return [(org, cid, ["corpus.read"], "active")]

    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    with caplog.at_level(logging.DEBUG):
        principal = await conduit_auth.validate_api_key(secret)

    # secret in neither the principal nor any captured log line
    assert secret not in principal.credential_id
    assert secret not in repr(principal)
    assert all(secret not in rec.getMessage() for rec in caplog.records)


async def test_get_principal_api_key_path(monkeypatch):
    """get_principal resolves via the X-API-Key header when no bearer is present."""
    org = uuid4()

    def _exec(sql, params):
        return [(org, "conduit:abc123def456", ["corpus.read"], "active")]

    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))
    request = SimpleNamespace(headers={"X-API-Key": "a-secret"})
    principal = await conduit_auth.get_principal(request)
    assert principal.org_id == str(org)
    assert principal.auth_kind == "api_key"


async def test_get_principal_no_credentials_raises_401(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(
            conduit_resource_uri="https://api.polarius.cz/conduit",
            conduit_oauth_issuer_url=None,
        ),
    )
    request = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as ei:
        await conduit_auth.get_principal(request)
    assert ei.value.status_code == 401
    assert "oauth-protected-resource" in ei.value.headers.get("WWW-Authenticate", "")
