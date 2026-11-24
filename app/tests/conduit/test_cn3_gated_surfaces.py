"""T-CN3 — the gated-surface Conduit code (locally unit-testable; live certification
DEFERRED).

Exercised with the Conduit service OFF and every engine/DB boundary mocked — no
listener, broker, network, AS, or live corpus. Coverage (manifest T-CN3 acceptance,
doc-05 §3 Tool 4/5, §4.2 OAuth, §5 metering, §6 tenancy):
  * tenancy: set_current_org is a NO-OP when rls_enforced=False; issues the org GUC
    FIRST (set_config) when rls_enforced=True; ConduitTenancyMiddleware publishes/
    resets the org contextvar.
  * list_obligations: cited corpus-level obligations / abstract framing (mock); zero
    coverage → refused.
  * check_compliance: a reserved-advice situation → refuse WITHOUT retrieval (no
    retrieval call), needs_lawyer=True, refusal_reason='reserved_advice'.
  * meter.record: ONE api_call(provider='conduit') + ONE conduit.call billing_event
    sharing the conduit_call_id; refused → amount 0, still recorded.
  * meter.precheck: pure pass-through when entitlements_enabled=False; over-quota → 429.
  * OAuth: wrong-aud → 401, missing-scope → 403, PRM doc shape (test JWKS).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from regsuper.conduit import auth as conduit_auth
from regsuper.conduit import meter, tools
from regsuper.conduit.envelope import RefusalReason
from regsuper.conduit.schemas import CheckComplianceInput, ListObligationsInput
from regsuper.conduit.tenancy import ConduitTenancyMiddleware, set_current_org


# ── shared fakes (mirror the CN2 suite's engine/DB doubles) ──────────────────
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
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0] if self._rows else None


def _fake_session_scope(execute_handler):
    """session_scope whose `.execute(sql, params)` delegates to
    `execute_handler(sql_str, params) -> rows`."""

    @asynccontextmanager
    async def _scope():
        class _S:
            async def execute(self, sql, params=None):
                return _FakeRows(execute_handler(str(sql), params or {}))

        yield _S()

    return _scope


# =============================================================================
# A. tenancy — set_current_org NO-OP-when-off / GUC-first-when-on
# =============================================================================
async def test_set_current_org_noop_when_rls_off(monkeypatch):
    """rls_enforced=False (the safe default) → set_current_org issues ZERO statements
    (byte-identical at rest, mirroring _apply_org_guc)."""
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(rls_enforced=False),
    )
    executed: list[str] = []

    class _S:
        async def execute(self, sql, params=None):
            executed.append(str(sql))

    await set_current_org(_S(), str(uuid4()))
    assert executed == []  # NO statement when RLS is off


async def test_set_current_org_issues_guc_first_when_rls_on(monkeypatch):
    """rls_enforced=True → set_current_org issues `set_config('app.current_org', :org,
    true)` FIRST (bound param, not interpolated), then pins is_platform_admin='off'."""
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(rls_enforced=True),
    )
    org = str(uuid4())
    executed: list[tuple[str, dict]] = []

    class _S:
        async def execute(self, sql, params=None):
            executed.append((str(sql), params or {}))

    await set_current_org(_S(), org)
    assert len(executed) == 2
    # GUC is the FIRST statement and carries the org as a BOUND param (no injection).
    first_sql, first_params = executed[0]
    assert "set_config('app.current_org'" in first_sql
    assert first_params == {"org": org}
    assert org not in first_sql  # the value is a param, not interpolated into the SQL
    # The second statement pins platform-admin OFF (a Conduit principal is never admin).
    second_sql, second_params = executed[1]
    assert "set_config('app.is_platform_admin'" in second_sql
    assert second_params == {"adm": "off"}


async def test_middleware_publishes_and_resets_org_contextvar():
    """ConduitTenancyMiddleware publishes the principal's org into current_org_var for
    the request and RESETS it after (no bleed across pooled connections)."""
    from regsuper.storage.database import current_org_var, is_platform_admin_var

    org = str(uuid4())
    seen: dict[str, object] = {}

    async def _inner_app(scope, receive, send):
        seen["org"] = current_org_var.get()
        seen["admin"] = is_platform_admin_var.get()

    mw = ConduitTenancyMiddleware(_inner_app)
    principal = SimpleNamespace(org_id=org)
    scope = {"type": "http", "state": {"conduit_principal": principal}}

    assert current_org_var.get() is None
    await mw(scope, None, None)
    assert seen["org"] == org  # published inside the request
    assert seen["admin"] is False  # Conduit principal pinned non-admin
    assert current_org_var.get() is None  # reset after the request (NULL-at-checkout)


async def test_middleware_noop_without_principal():
    """A public/unauthenticated request (no principal in scope state) publishes NO
    org — the GUC is never issued for public routes (healthz / PRM)."""
    from regsuper.storage.database import current_org_var

    async def _inner_app(scope, receive, send):
        assert current_org_var.get() is None  # nothing published

    mw = ConduitTenancyMiddleware(_inner_app)
    await mw({"type": "http", "state": {}}, None, None)
    assert current_org_var.get() is None


# =============================================================================
# B. list_obligations — cited corpus-level obligations / abstract framing
# =============================================================================
async def test_list_obligations_returns_cited_obligations(monkeypatch):
    """A profile → corpus-level obligations, each carrying a citation; only provisions
    the corpus MARKED with a deontic modal become obligations."""
    pid_obl, pid_no_modal = uuid4(), uuid4()
    result = _FakeResult(
        candidates=[_FakeHit(pid_obl, 0.9), _FakeHit(pid_no_modal, 0.7)],
        payloads={
            pid_obl: {
                "label": "§ 5",
                "external_id": "563/1991",
                "title": "Zákon o účetnictví",
                "legal_hierarchy": "cz-act",
                "jurisdiction": "CZ",
                "text": "...",
            },
            pid_no_modal: {
                "label": "§ 1",
                "external_id": "563/1991",
                "title": "Zákon o účetnictví",
                "legal_hierarchy": "cz-act",
                "jurisdiction": "CZ",
                "text": "...",
            },
        },
        top_rerank_score=0.9,
    )

    async def _fake_retrieve(query, **kwargs):
        return result

    def _exec(sql, params):
        # the _deontic_for SELECT — pid_obl has a modal, pid_no_modal does not.
        return [
            (
                pid_obl,
                "shall",
                "účetní jednotka",
                "vést účetnictví",
                None,
                "§ 5",
                "563/1991",
                date(1992, 1, 1),
                None,
            ),
            (pid_no_modal, None, None, None, None, "§ 1", "563/1991", None, None),
        ]

    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    env = await tools.list_obligations(ListObligationsInput(sector="accounting", entity_type="sro"))
    assert env.refused is False
    assert env.data["n_obligations"] == 1  # only the modal-marked provision
    obl = env.data["obligations"][0]
    assert obl["modal"] == "shall"
    assert obl["provision_id"] == str(pid_obl)
    assert obl["actor"] == "účetní jednotka"
    # every obligation is cited through finalize
    assert len(env.citations) == 1
    assert env.citations[0].provision_id == str(pid_obl)
    assert "not legal advice" in env.disclaimer


async def test_list_obligations_no_marked_obligations_refuses(monkeypatch):
    """Candidates exist but NONE carry a deontic modal → refused no_coverage (never an
    uncited / fabricated obligation)."""
    pid = uuid4()
    result = _FakeResult(
        candidates=[_FakeHit(pid, 0.8)],
        payloads={pid: {"label": "§ 1", "external_id": "A", "text": "...", "jurisdiction": "CZ"}},
        top_rerank_score=0.8,
    )

    async def _fake_retrieve(query, **kwargs):
        return result

    def _exec(sql, params):
        return [(pid, None, None, None, None, "§ 1", "A", None, None)]  # no modal

    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    env = await tools.list_obligations(ListObligationsInput(sector="x", entity_type="y"))
    assert env.refused is True
    assert env.refusal_reason is RefusalReason.NO_COVERAGE
    assert env.data is None


# =============================================================================
# C. check_compliance — reserved-advice refuses WITHOUT retrieval
# =============================================================================
async def test_check_compliance_reserved_advice_refuses_without_retrieval(monkeypatch):
    """A reserved-advice situation → refuse with needs_lawyer=True WITHOUT calling
    retrieval (no engine work; the hand-off pointer is populated)."""
    retrieve_called = {"n": 0}

    async def _verdict(*, text, where, **kwargs):
        assert where == "input"  # pre-gate runs on the input, before any retrieval
        return SimpleNamespace(answer_mode="reserved_advice")

    async def _fake_retrieve(*a, **k):
        retrieve_called["n"] += 1
        raise AssertionError("retrieval must NOT run on a reserved-advice refusal")

    monkeypatch.setattr("regsuper.analysis.compliance_guard.classify_answer_mode", _verdict)
    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)

    env = await tools.check_compliance(
        CheckComplianceInput(situation="I'm being sued by X, represent me and file for me")
    )
    assert env.refused is True
    assert env.needs_lawyer is True
    assert env.refusal_reason is RefusalReason.RESERVED_ADVICE
    assert env.data is None  # finalize forces data=None on a refusal
    assert retrieve_called["n"] == 0  # NO retrieval call → no retrieval.* api_call


async def test_check_compliance_needs_review_also_refuses(monkeypatch):
    """The fail-closed NEEDS_REVIEW verdict is folded into the reserved-advice refusal
    (the conservative path, doc-05 §7.2)."""

    async def _verdict(*, text, where, **kwargs):
        return SimpleNamespace(answer_mode="needs_review")

    monkeypatch.setattr("regsuper.analysis.compliance_guard.classify_answer_mode", _verdict)

    env = await tools.check_compliance(CheckComplianceInput(situation="ambiguous"))
    assert env.refused is True
    assert env.needs_lawyer is True
    assert env.refusal_reason is RefusalReason.RESERVED_ADVICE


async def test_check_compliance_information_runs_retrieval_and_cites(monkeypatch):
    """An INFORMATION situation runs retrieval and surfaces cited governing provisions
    (+ general obligations from their deontic fields)."""
    pid = uuid4()
    result = _FakeResult(
        candidates=[_FakeHit(pid, 0.9)],
        payloads={
            pid: {
                "label": "Art 6",
                "external_id": "GDPR",
                "title": "GDPR",
                "legal_hierarchy": "eu-regulation",
                "jurisdiction": "EU",
                "text": "Processing shall be lawful.",
            }
        },
        top_rerank_score=0.9,
    )

    async def _verdict(*, text, where, **kwargs):
        return SimpleNamespace(answer_mode="information")

    async def _fake_retrieve(query, **kwargs):
        return result

    def _exec(sql, params):
        return [
            (
                pid,
                "shall",
                "controller",
                "ensure lawful processing",
                None,
                "Art 6",
                "GDPR",
                None,
                None,
            )
        ]

    monkeypatch.setattr("regsuper.analysis.compliance_guard.classify_answer_mode", _verdict)
    monkeypatch.setattr("regsuper.retrieval.retrieve", _fake_retrieve)
    monkeypatch.setattr("regsuper.storage.database.session_scope", _fake_session_scope(_exec))

    env = await tools.check_compliance(
        CheckComplianceInput(situation="what does GDPR require for lawful processing")
    )
    assert env.refused is False
    assert env.needs_lawyer is False
    assert env.data["lawyer_handoff"] is None
    assert env.data["governing_provisions"][0]["provision_id"] == str(pid)
    assert env.data["general_obligations"][0]["modal"] == "shall"
    assert len(env.citations) == 1


# =============================================================================
# D. meter — precheck pass-through / over-quota; record one+one sharing call id
# =============================================================================
async def test_precheck_passthrough_when_entitlements_off(monkeypatch):
    """entitlements_enabled=False (safe default) → precheck reserves NO quota and
    returns allowed=True with a minted conduit_call_id (byte-identical at rest)."""
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(entitlements_enabled=False),
    )
    res = await meter.precheck(str(uuid4()), "search_czech_law")
    assert res.allowed is True
    # a real uuid4 string was minted
    assert UUID(res.conduit_call_id)


async def test_precheck_over_quota_raises_429(monkeypatch):
    """entitlements_enabled=True + over-quota → QuotaExceeded(429) propagates from
    precheck BEFORE any engine call."""
    from regsuper.observability.limits import QuotaExceeded

    org = uuid4()
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(entitlements_enabled=True),
    )

    async def _fake_ent(session, org_id):
        return SimpleNamespace(queries_per_day=3)

    async def _fake_reserve(session, org_id, metric, *, limit, **kwargs):
        raise QuotaExceeded(metric, limit, count=4)

    monkeypatch.setattr("regsuper.entitlements.resolver.effective_entitlement", _fake_ent)
    monkeypatch.setattr("regsuper.observability.limits.check_and_reserve_quota", _fake_reserve)
    monkeypatch.setattr(
        "regsuper.storage.database.session_scope", _fake_session_scope(lambda s, p: [])
    )

    with pytest.raises(QuotaExceeded) as ei:
        await meter.precheck(str(org), "search_czech_law")
    assert ei.value.status_code == 429
    assert ei.value.code == "quota.exceeded"


async def test_record_writes_one_api_call_and_one_billing_event(monkeypatch):
    """meter.record writes EXACTLY ONE api_call(provider='conduit') + ONE conduit.call
    billing_event, both sharing the SAME conduit_call_id."""
    org = uuid4()
    call_id = str(uuid4())
    added: list = []
    emitted: list[dict] = []

    class _S:
        def add(self, row):
            added.append(row)

        async def flush(self):
            pass

        async def execute(self, sql, params=None):
            return _FakeRows([])

    @asynccontextmanager
    async def _scope():
        yield _S()

    async def _fake_emit(
        session,
        org_id,
        event_type,
        *,
        conduit_call_id=None,
        amount_usd=None,
        currency=None,
        payload=None,
        **kwargs,
    ):
        emitted.append(
            {
                "org_id": org_id,
                "event_type": event_type,
                "conduit_call_id": conduit_call_id,
                "amount_usd": amount_usd,
                "payload": payload,
            }
        )

    monkeypatch.setattr("regsuper.storage.database.session_scope", _scope)
    monkeypatch.setattr("regsuper.billing.events.emit_billing_event", _fake_emit)

    await meter.record(str(org), "search_czech_law", call_id, refused=False, amount_usd=0.01)

    # exactly one api_call row, tagged provider='conduit', org set, no raw query text
    assert len(added) == 1
    api_row = added[0]
    assert api_row.provider == "conduit"
    assert api_row.source == "conduit.search_czech_law"
    assert api_row.org_id == org
    assert "query" not in api_row.request_json  # data minimisation (doc-05 §7.4)
    assert api_row.request_json["conduit_call_id"] == call_id

    # exactly one conduit.call billing_event SHARING the conduit_call_id
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev["event_type"] == "conduit.call"
    assert ev["org_id"] == org
    assert str(ev["conduit_call_id"]) == call_id  # SAME id links the pair
    assert ev["payload"]["tool"] == "search_czech_law"


async def test_record_refused_bills_zero_still_recorded(monkeypatch):
    """A refused call records at amount_usd=0 but is STILL written (abuse/funnel
    visible) — both rows present, billing amount is zero."""
    org = uuid4()
    call_id = str(uuid4())
    added: list = []
    emitted: list[dict] = []

    class _S:
        def add(self, row):
            added.append(row)

        async def flush(self):
            pass

        async def execute(self, sql, params=None):
            return _FakeRows([])

    @asynccontextmanager
    async def _scope():
        yield _S()

    async def _fake_emit(
        session,
        org_id,
        event_type,
        *,
        conduit_call_id=None,
        amount_usd=None,
        currency=None,
        payload=None,
        **kwargs,
    ):
        emitted.append({"event_type": event_type, "amount_usd": amount_usd, "payload": payload})

    monkeypatch.setattr("regsuper.storage.database.session_scope", _scope)
    monkeypatch.setattr("regsuper.billing.events.emit_billing_event", _fake_emit)

    # a refused call still records (amount ignored → 0)
    await meter.record(str(org), "check_compliance", call_id, refused=True, amount_usd=5.0)

    assert len(added) == 1
    assert added[0].status == "refused"
    assert len(emitted) == 1
    assert str(emitted[0]["amount_usd"]) == "0"  # refused → 0 (Decimal('0'))
    assert emitted[0]["payload"]["refused"] is True


# =============================================================================
# E. OAuth RS — wrong-aud → 401, missing-scope → 403, PRM shape (test JWKS)
# =============================================================================
def _make_token(*, claims, kid="test-key"):
    """Mint an RS256 JWT signed by a freshly-generated RSA key; return (token, jwks).
    Skips when pyjwt[crypto] (the optional `conduit` extra) is absent."""
    jwt = pytest.importorskip("jwt")
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})
    # Build the matching JWKS public-key set.
    from jwt.algorithms import RSAAlgorithm

    pub_jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    pub_jwk["kid"] = kid
    return token, {"keys": [pub_jwk]}


def _patch_jwks(monkeypatch, jwks):
    """Patch PyJWKClient so token validation uses our in-memory test JWKS (no network)."""
    pytest.importorskip("jwt")

    class _FakeJWKClient:
        def __init__(self, *a, **k):
            pass

        def get_signing_key_from_jwt(self, token):
            from jwt import PyJWK

            return PyJWK(jwks["keys"][0])

    monkeypatch.setattr("jwt.PyJWKClient", _FakeJWKClient)


_RESOURCE = "https://mcp.polarius.cz/mcp"
_ISSUER = "https://as.polarius.cz/"


def _oauth_settings(**over):
    base = dict(
        conduit_oauth_issuer_url=_ISSUER,
        conduit_oauth_jwks_url="https://as.polarius.cz/jwks",
        conduit_resource_uri=_RESOURCE,
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_oauth_wrong_aud_rejected_401(monkeypatch):
    """A token whose `aud` != the canonical resource is rejected 401 (the central
    confused-deputy defence, doc-05 §4.2 step 2)."""
    from fastapi import HTTPException

    pytest.importorskip("jwt")
    import time

    token, jwks = _make_token(
        claims={
            "aud": "https://evil.example/mcp",  # WRONG audience
            "iss": _ISSUER,
            "org_id": str(uuid4()),
            "scope": "corpus.read",
            "exp": int(time.time()) + 3600,
        }
    )
    monkeypatch.setattr("regsuper.config.get_settings", _oauth_settings)
    _patch_jwks(monkeypatch, jwks)

    with pytest.raises(HTTPException) as ei:
        await conduit_auth.validate_oauth_token(token)
    assert ei.value.status_code == 401


async def test_oauth_valid_token_resolves_principal(monkeypatch):
    """A valid token (correct aud/iss/org_id) → a ConduitPrincipal with the projected
    scopes; auth_kind='oauth', no secret stored."""
    pytest.importorskip("jwt")
    import time

    org = str(uuid4())
    token, jwks = _make_token(
        claims={
            "aud": _RESOURCE,
            "iss": _ISSUER,
            "org_id": org,
            "scope": "corpus.read obligations.read bogus.scope",
            "exp": int(time.time()) + 3600,
        }
    )
    monkeypatch.setattr("regsuper.config.get_settings", _oauth_settings)
    _patch_jwks(monkeypatch, jwks)

    principal = await conduit_auth.validate_oauth_token(token)
    assert principal.org_id == org
    assert principal.auth_kind == "oauth"
    # bogus.scope is dropped (projected onto the recognised Conduit scope set)
    assert principal.scopes == frozenset({"corpus.read", "obligations.read"})


async def test_oauth_missing_scope_403(monkeypatch):
    """require_scope: a principal lacking the tool's required scope → 403 (step 4)."""
    from fastapi import HTTPException

    # principal holds only corpus.read; answer_legal_question needs answer.generate.
    principal = conduit_auth.ConduitPrincipal(
        org_id=str(uuid4()),
        credential_id="conduit:org:x",
        scopes=frozenset({"corpus.read"}),
        auth_kind="oauth",
    )
    with pytest.raises(HTTPException) as ei:
        conduit_auth.require_scope(principal, "answer_legal_question")
    assert ei.value.status_code == 403

    # but a tool whose scope it DOES hold passes (no raise)
    conduit_auth.require_scope(principal, "search_czech_law")


async def test_oauth_unknown_tool_fails_closed_403(monkeypatch):
    """require_scope: an UNKNOWN tool requires the FULL scope set (fail-closed) — a
    partially-scoped principal is denied 403, never silently granted."""
    from fastapi import HTTPException

    principal = conduit_auth.ConduitPrincipal(
        org_id=str(uuid4()),
        credential_id="conduit:org:x",
        scopes=frozenset({"corpus.read"}),
    )
    with pytest.raises(HTTPException) as ei:
        conduit_auth.require_scope(principal, "not_a_real_tool")
    assert ei.value.status_code == 403


async def test_oauth_validate_falls_through_when_as_unconfigured(monkeypatch):
    """No AS configured (issuer/jwks None) → validate_oauth_token raises
    NotImplementedError so get_principal falls through to the API-key channel
    (deroute-D)."""
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: SimpleNamespace(
            conduit_oauth_issuer_url=None,
            conduit_oauth_jwks_url=None,
            conduit_resource_uri=_RESOURCE,
        ),
    )
    with pytest.raises(NotImplementedError):
        await conduit_auth.validate_oauth_token("any.bearer.token")


def test_prm_document_shape(monkeypatch):
    """protected_resource_metadata returns a valid RFC 9728 doc: resource = canonical
    uri, authorization_servers lists the configured AS, scopes_supported is the full
    Conduit scope set, bearer_methods_supported=['header']."""
    monkeypatch.setattr("regsuper.config.get_settings", _oauth_settings)
    prm = conduit_auth.protected_resource_metadata()
    assert prm["resource"] == _RESOURCE
    assert prm["authorization_servers"] == [_ISSUER]
    assert set(prm["scopes_supported"]) == {
        "corpus.read",
        "obligations.read",
        "compliance.check",
        "answer.generate",
    }
    assert prm["bearer_methods_supported"] == ["header"]


def test_prm_authorization_servers_empty_when_as_unconfigured(monkeypatch):
    """No AS issuer → authorization_servers is [] (honest, never a fabricated AS); the
    document shape is still valid and the API-key channel ships regardless."""
    monkeypatch.setattr(
        "regsuper.config.get_settings",
        lambda: _oauth_settings(conduit_oauth_issuer_url=None),
    )
    prm = conduit_auth.protected_resource_metadata()
    assert prm["resource"] == _RESOURCE
    assert prm["authorization_servers"] == []
